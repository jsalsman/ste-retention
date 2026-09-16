"""Best-effort filesystem leases for resumable experiment coordination."""

import fcntl
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO
from uuid import uuid4

# Lease timestamps support stale-run recovery when a FUSE driver cannot honor flock.
LEASE_SECONDS = 120
_FALLBACK_LOCK = threading.Lock()


class RunActiveError(RuntimeError):
    """Report that another request currently owns a live experiment lease."""


def _read_metadata(handle: TextIO) -> dict:
    """Read safe lease metadata, treating empty or damaged files as stale leases."""
    handle.seek(0)
    try:
        # A malformed lock file must not permanently prevent recovery.
        value = json.load(handle)
        # Only an object can contain the expected owner and timestamp fields.
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _write_metadata(handle: TextIO, owner_id: str, expires_at: datetime) -> None:
    """Replace timestamp metadata through an already-open lease file handle."""
    handle.seek(0)
    handle.truncate()
    # No credential or request content is ever written to coordination files.
    json.dump(
        {
            "owner_id": owner_id,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "lease_expires_at": expires_at.isoformat(),
        },
        handle,
        separators=(",", ":"),
    )
    handle.flush()
    try:
        # Some FUSE implementations accept fsync while others report it unsupported.
        os.fsync(handle.fileno())
    except OSError:
        pass


@dataclass
class RunLease:
    """Hold an open lease file and its best available filesystem lock."""

    run_id: str
    owner_id: str
    expires_at: datetime
    handle: TextIO
    flock_acquired: bool

    def heartbeat(self) -> datetime:
        """Extend this request's timestamped lease before saving a checkpoint."""
        expires = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
        if not self.flock_acquired:
            with _FALLBACK_LOCK:
                metadata = _read_metadata(self.handle)
                # Detect ownership changes even when only timestamp fallback is available.
                if metadata.get("owner_id") != self.owner_id:
                    raise RunActiveError("Experiment ownership changed; this request stopped.")
                _write_metadata(self.handle, self.owner_id, expires)
        else:
            # The held exclusive flock serializes ordinary filesystem writers.
            _write_metadata(self.handle, self.owner_id, expires)
        self.expires_at = expires
        return expires

    def release(self) -> None:
        """Expire metadata, then best-effort unlock and close this lease handle."""
        try:
            # An immediate expiry lets a later request recover after graceful termination.
            _write_metadata(self.handle, self.owner_id, datetime.now(timezone.utc))
            if self.flock_acquired:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()


def acquire_lease(run_id: str, directory: Path) -> RunLease:
    """Acquire an ordinary nonblocking file lock with a timestamp fallback for FUSE."""
    lease_directory = directory / "leases"
    lease_directory.mkdir(parents=True, exist_ok=True)
    handle = (lease_directory / f"{run_id}.lock").open("a+", encoding="utf-8")
    owner_id = uuid4().hex
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=LEASE_SECONDS)
    try:
        # flock is reliable on local filesystems and attempted first on the mounted path.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        flock_acquired = True
    except BlockingIOError as exc:
        handle.close()
        raise RunActiveError("This experiment is still running in another request.") from exc
    except OSError:
        # GCS FUSE can reject locking; timestamps then provide best-effort stale detection.
        flock_acquired = False
        with _FALLBACK_LOCK:
            metadata = _read_metadata(handle)
            expiry_text = metadata.get("lease_expires_at")
            try:
                previous_expiry = datetime.fromisoformat(expiry_text) if expiry_text else None
            except (TypeError, ValueError):
                previous_expiry = None
            if previous_expiry and previous_expiry > now:
                handle.close()
                raise RunActiveError(
                    "This experiment is still running in another request."
                ) from None
    _write_metadata(handle, owner_id, expires)
    return RunLease(run_id, owner_id, expires, handle, flock_acquired)
