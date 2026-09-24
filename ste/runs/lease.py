"""Generation-conditioned Cloud Storage leases for web experiment runs."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
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
    """Own one generation-fenced Cloud Storage lease object."""

    run_id: str
    owner_id: str
    expires_at: datetime
    duration_seconds: float
    backend: StorageBackend
    generation: int
    object_name: str

    def heartbeat(self) -> datetime:
        """Extend ownership, rejecting a lease stolen by another request."""
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.duration_seconds)
        blob = self.backend.bucket.blob(self.object_name)
        try:
            # The generation precondition proves this request still owns the lease.
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
            # A stale owner cannot delete the winner's newer generation.
            self.backend.bucket.blob(self.object_name).delete(if_generation_match=self.generation)
        except Exception:
            # Generator cleanup cannot recover or safely report release failures.
            return


def _read_gcs_lease(bucket: Any, object_name: str) -> tuple[dict, int]:
    """Read fresh lease metadata and its generation through the object API."""
    contents, generation = read_blob(bucket, object_name)
    # Parsing separately lets malformed content participate in fenced takeover.
    return _parse_metadata(contents), generation


def _adopt_ambiguous_acquire(bucket: Any, object_name: str, owner_id: str) -> int:
    """Adopt an acquire upload that succeeded before its response was lost.

    A client retry can surface ``PreconditionFailed`` after the original conditional
    upload committed. Only the freshly read owner identifier can distinguish that
    result from contention, and its generation becomes the caller's fencing token.
    """
    try:
        # Read through the API so a mounted metadata cache cannot hide the committed owner.
        metadata, generation = _read_gcs_lease(bucket, object_name)
    except Exception as exc:
        # Without current ownership proof, the request must not proceed with paid work.
        raise RunActiveError(ACTIVE_MESSAGE) from exc
    if metadata.get("owner_id") != owner_id:
        # A different owner won the retry race and retains the active lease.
        raise RunActiveError(ACTIVE_MESSAGE)
    return generation


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
                        # The retried create may have committed before its response was lost.
                        generation = _adopt_ambiguous_acquire(backend.bucket, object_name, owner_id)
                    else:
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
                        # A committed takeover owns a newer generation despite this response.
                        generation = _adopt_ambiguous_acquire(backend.bucket, object_name, owner_id)
                    else:
                        raise LeaseUnavailableError(
                            "Cloud Storage lease takeover failed."
                        ) from takeover_error
    return RunLease(
        run_id,
        owner_id,
        expires,
        duration,
        backend,
        generation,
        object_name,
    )


def acquire_lease(
    run_id: str, directory: Path, *, duration_seconds: float | None = None
) -> RunLease:
    """Acquire a generation-conditioned lease for an experiment run identifier."""
    owner_id = uuid4().hex
    # Compute ownership identity before any create, enabling ambiguous retry recovery.
    duration = duration_seconds if duration_seconds is not None else lease_duration()
    backend = get_backend(directory)
    # All web coordination goes through the API; the mounted path is never locked.
    return _acquire_gcs(run_id, backend, owner_id, duration)
