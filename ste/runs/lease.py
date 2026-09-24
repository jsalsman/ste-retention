"""Exclusive local and generation-conditioned Cloud Storage run leases."""

import fcntl
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from ste.runs.backend import (
    LeaseUnavailableError,
    StorageBackend,
    get_backend,
    is_not_found,
    is_precondition_failed,
    read_blob,
)

# Leases exceed the longest paid unit by this margin, while takeover tolerates skew.
LEASE_SECONDS = 120
LEASE_SAFETY_SECONDS = 15
LEASE_SKEW_SECONDS = 5
ACTIVE_MESSAGE = "This experiment is still running in another request."
LOST_MESSAGE = "Experiment ownership changed; this request stopped."


class RunActiveError(RuntimeError):
    """Report that another request currently owns a live experiment lease."""


def lease_duration(*timeouts: float) -> float:
    """Return a lease duration exceeding both the baseline and longest work unit."""
    # A checkpoint cannot run during one provider or judge call, so cover that gap.
    longest = max((float(value) for value in timeouts), default=0.0)
    return max(float(LEASE_SECONDS), longest) + LEASE_SAFETY_SECONDS


def _metadata(owner_id: str, expires_at: datetime) -> bytes:
    """Serialize exactly the public lease ownership and heartbeat fields."""
    # Compact deterministic JSON minimizes the coordination object size.
    value = {
        "owner_id": owner_id,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "lease_expires_at": expires_at.isoformat(),
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _parse_metadata(contents: bytes) -> dict:
    """Decode lease metadata, treating empty or malformed values as stale."""
    try:
        # Only an object can contain all fields required for ownership decisions.
        value = json.loads(contents)
        return value if isinstance(value, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _expiry(metadata: dict) -> datetime | None:
    """Return a timezone-aware expiry or mark malformed metadata as stale."""
    try:
        # Naive values cannot safely be compared between instances and are stale.
        value = datetime.fromisoformat(metadata["lease_expires_at"])
        return value if value.tzinfo is not None else None
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class RunLease:
    """Own a local descriptor lock or a generation-fenced GCS lease object."""

    run_id: str
    owner_id: str
    expires_at: datetime
    duration_seconds: float
    backend: StorageBackend
    handle: TextIO | None = None
    generation: int | None = None
    object_name: str | None = None

    def heartbeat(self) -> datetime:
        """Extend ownership, rejecting a lease stolen by another request."""
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.duration_seconds)
        if not self.backend.is_gcs:
            # The held descriptor lock is the local backend's ownership proof.
            _write_local_metadata(self.handle, self.owner_id, expires)
        else:
            blob = self.backend.bucket.blob(self.object_name)
            try:
                blob.upload_from_string(
                    _metadata(self.owner_id, expires),
                    content_type="application/json",
                    if_generation_match=self.generation,
                )
                self.generation = int(blob.generation)
            except Exception as exc:
                if not is_precondition_failed(exc):
                    raise LeaseUnavailableError("Cloud Storage lease renewal failed.") from exc
                self._adopt_ambiguous_heartbeat()
        self.expires_at = expires
        return expires

    def _adopt_ambiguous_heartbeat(self) -> None:
        """Adopt an ambiguous successful retry or report changed ownership."""
        try:
            contents, generation = read_blob(self.backend.bucket, self.object_name)
        except Exception as exc:
            raise RunActiveError(LOST_MESSAGE) from exc
        # A retry can report a failed old precondition after its first write succeeded.
        if _parse_metadata(contents).get("owner_id") != self.owner_id:
            raise RunActiveError(LOST_MESSAGE)
        self.generation = generation

    def release(self) -> None:
        """Release ownership without ever raising from request cleanup paths."""
        try:
            if self.backend.is_gcs:
                # A stale owner cannot delete the winner's newer generation.
                self.backend.bucket.blob(self.object_name).delete(
                    if_generation_match=self.generation
                )
            elif self.handle is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            # Generator cleanup cannot recover or safely report release failures.
            with suppress(Exception):
                if self.handle is not None:
                    self.handle.close()
        finally:
            # Closing a local descriptor also releases its kernel lock.
            if self.handle is not None:
                self.handle.close()


def _write_local_metadata(handle: TextIO | None, owner_id: str, expires_at: datetime) -> None:
    """Write diagnostic liveness metadata while an exclusive local lock is held."""
    if handle is None:
        raise LeaseUnavailableError("Local experiment lease is unavailable.")
    handle.seek(0)
    handle.truncate()
    # Local metadata supports status reporting; flock alone establishes ownership.
    handle.write(_metadata(owner_id, expires_at).decode("utf-8"))
    handle.flush()


def _acquire_local(
    run_id: str, directory: Path, backend: StorageBackend, owner_id: str, duration: float
) -> RunLease:
    """Acquire one exclusive nonblocking kernel lock on a local filesystem."""
    lease_directory = directory / "leases"
    lease_directory.mkdir(parents=True, exist_ok=True)
    handle = (lease_directory / f"{run_id}.lock").open("a+", encoding="utf-8")
    try:
        # A filesystem that rejects flock cannot provide local exclusive ownership.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RunActiveError(ACTIVE_MESSAGE) from exc
    except Exception as exc:
        handle.close()
        raise LeaseUnavailableError("Exclusive experiment locking is unavailable.") from exc
    expires = datetime.now(timezone.utc) + timedelta(seconds=duration)
    _write_local_metadata(handle, owner_id, expires)
    return RunLease(run_id, owner_id, expires, duration, backend, handle=handle)


def _read_gcs_lease(bucket: Any, object_name: str) -> tuple[dict, int]:
    """Read fresh lease metadata and its generation through the object API."""
    contents, generation = read_blob(bucket, object_name)
    # Parsing separately lets malformed content participate in fenced takeover.
    return _parse_metadata(contents), generation


def _acquire_gcs(run_id: str, backend: StorageBackend, owner_id: str, duration: float) -> RunLease:
    """Create or conditionally take over a Cloud Storage lease object."""
    object_name = backend.object_name(f"leases/{run_id}.lock")
    expires = datetime.now(timezone.utc) + timedelta(seconds=duration)
    blob = backend.bucket.blob(object_name)
    try:
        blob.upload_from_string(
            _metadata(owner_id, expires), content_type="application/json", if_generation_match=0
        )
        generation = int(blob.generation)
    except Exception as create_error:
        if not is_precondition_failed(create_error):
            raise LeaseUnavailableError("Cloud Storage lease acquisition failed.") from create_error
        try:
            metadata, generation = _read_gcs_lease(backend.bucket, object_name)
        except Exception as read_error:
            if is_not_found(read_error) or isinstance(read_error, FileNotFoundError):
                # The prior owner released between create and read; retry creation once.
                try:
                    blob.upload_from_string(
                        _metadata(owner_id, expires),
                        content_type="application/json",
                        if_generation_match=0,
                    )
                    generation = int(blob.generation)
                except Exception as retry_error:
                    if is_precondition_failed(retry_error):
                        raise RunActiveError(ACTIVE_MESSAGE) from retry_error
                    raise LeaseUnavailableError(
                        "Cloud Storage lease acquisition failed."
                    ) from retry_error
            else:
                raise LeaseUnavailableError("Cloud Storage lease read failed.") from read_error
        else:
            if metadata.get("owner_id") == owner_id:
                # The original create succeeded but its response was ambiguous.
                pass
            else:
                previous_expiry = _expiry(metadata)
                stale_before = datetime.now(timezone.utc) - timedelta(seconds=LEASE_SKEW_SECONDS)
                if previous_expiry is not None and previous_expiry >= stale_before:
                    raise RunActiveError(ACTIVE_MESSAGE)
                try:
                    blob.upload_from_string(
                        _metadata(owner_id, expires),
                        content_type="application/json",
                        if_generation_match=generation,
                    )
                    generation = int(blob.generation)
                except Exception as takeover_error:
                    if is_precondition_failed(takeover_error):
                        raise RunActiveError(ACTIVE_MESSAGE) from takeover_error
                    raise LeaseUnavailableError(
                        "Cloud Storage lease takeover failed."
                    ) from takeover_error
    return RunLease(
        run_id,
        owner_id,
        expires,
        duration,
        backend,
        generation=generation,
        object_name=object_name,
    )


def acquire_lease(
    run_id: str, directory: Path, *, duration_seconds: float | None = None
) -> RunLease:
    """Acquire the safe backend-specific lease for an experiment run identifier."""
    owner_id = uuid4().hex
    # Compute ownership identity before any create, enabling ambiguous retry recovery.
    duration = duration_seconds if duration_seconds is not None else lease_duration()
    backend = get_backend(directory)
    if backend.is_gcs:
        # Never call flock on FUSE: a successful kernel lock is only process-local.
        return _acquire_gcs(run_id, backend, owner_id, duration)
    return _acquire_local(run_id, directory, backend, owner_id, duration)
