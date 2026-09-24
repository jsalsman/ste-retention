"""Detect and verify the Cloud Storage mount required by web run coordination."""

import re
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

# Linux mountinfo escapes whitespace and a few path characters as octal bytes.
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
# Detection and the required mapping probe run once per resolved directory per process.
_BACKENDS: dict[Path, "StorageBackend"] = {}
_BACKEND_ERRORS: dict[Path, str] = {}
_BACKENDS_LOCK = threading.Lock()
_CLIENT_FACTORY: Callable[[], Any] | None = None


class LeaseUnavailableError(RuntimeError):
    """Report that safe cross-request coordination cannot be established."""


@dataclass(frozen=True)
class GCSMount:
    """Describe the bucket and object prefix backing one gcsfuse mount."""

    mount_point: Path
    bucket_name: str
    prefix: str


@dataclass(frozen=True)
class StorageBackend:
    """Describe a verified Cloud Storage bucket and mounted object-path mapping."""

    directory: Path
    mount: GCSMount
    bucket: Any

    def object_name(self, relative_name: str) -> str:
        """Return the bucket object name for a path relative to EXPERIMENTS_DIR."""
        # The verified prefix maps EXPERIMENTS_DIR-relative names into the bucket.
        return f"{self.mount.prefix}{relative_name}"


def _decode_mount_field(value: str) -> str:
    """Decode Linux mountinfo octal escapes without interpreting other backslashes."""
    # mountinfo represents spaces as ``\040`` and backslashes as ``\134``.
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def detect_gcs_mount(
    directory: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")
) -> GCSMount | None:
    """Find the longest gcsfuse mount containing a resolved persistence directory.

    Malformed and unrelated lines are ignored. The mount point is field five on
    the left of the separator; filesystem type and bucket source begin the right.
    """
    target = directory.resolve()
    try:
        # Tests can provide an ordinary fixture file instead of relying on /proc.
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    matches: list[tuple[int, GCSMount]] = []
    for line in lines:
        # The separator makes optional mount fields safe to skip.
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields, right_fields = left.split(), right.split()
        if len(left_fields) < 5 or len(right_fields) < 2 or right_fields[0] != "fuse.gcsfuse":
            continue
        mount_point = Path(_decode_mount_field(left_fields[4])).resolve()
        try:
            relative = target.relative_to(mount_point)
        except ValueError:
            # A lexical sibling such as /experiments-old is not inside the mount.
            continue
        prefix = "" if relative == Path(".") else f"{relative.as_posix().rstrip('/')}/"
        matches.append(
            (
                len(mount_point.parts),
                GCSMount(mount_point, _decode_mount_field(right_fields[1]), prefix),
            )
        )
    # Nested mounts override broader mounts, matching the kernel's path selection.
    return max(matches, key=lambda item: item[0])[1] if matches else None


def _new_client() -> Any:
    """Create an ADC-backed client lazily after a gcsfuse mount is detected."""
    if _CLIENT_FACTORY is not None:
        # Tests inject an in-memory client and never contact the network.
        return _CLIENT_FACTORY()
    from google.cloud import storage

    return storage.Client()


def _exception_named(exc: BaseException, name: str) -> bool:
    """Recognize Google API errors while allowing lightweight test doubles."""
    # Lazy exception recognition avoids importing the cloud package locally.
    return any(cls.__name__ == name for cls in type(exc).__mro__)


def is_precondition_failed(exc: BaseException) -> bool:
    """Return whether an exception represents a failed generation precondition."""
    # Both google.api_core and the in-memory fake use this stable class name.
    return _exception_named(exc, "PreconditionFailed")


def is_not_found(exc: BaseException) -> bool:
    """Return whether an exception represents an absent Cloud Storage object."""
    # Do not inspect or expose exception text, which could contain object details.
    return _exception_named(exc, "NotFound")


def read_blob(bucket: Any, name: str) -> tuple[bytes, int]:
    """Read current object bytes and generation without using the mounted cache."""
    blob = bucket.get_blob(name)
    if blob is None:
        # A small local exception keeps the cloud dependency lazy in tests.
        raise FileNotFoundError(name)
    generation = int(blob.generation)
    # Pin the download to the generation returned by the metadata read.
    data = blob.download_as_bytes(if_generation_match=generation)
    return data, generation


def _verify_mapping(directory: Path, mount: GCSMount, bucket: Any) -> None:
    """Verify that a mount write appears at the computed bucket object name."""
    token = uuid4().hex
    relative_name = f"leases/.mapping-probe-{uuid4().hex}"
    path = directory / relative_name
    object_name = f"{mount.prefix}{relative_name}"
    generation: int | None = None
    try:
        # Closing the mounted file makes the probe eligible for API visibility.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(token.encode("ascii"))
        contents, generation = read_blob(bucket, object_name)
        if contents != token.encode("ascii"):
            raise LeaseUnavailableError("Cloud Storage mount verification failed.")
    except LeaseUnavailableError:
        raise
    except Exception as exc:
        raise LeaseUnavailableError("Cloud Storage mount verification failed.") from exc
    finally:
        try:
            # Delete through the API with fencing; mounted cleanup is only a fallback.
            if generation is not None:
                bucket.blob(object_name).delete(if_generation_match=generation)
            elif path.exists():
                path.unlink()
        except Exception:
            # Verification already failed or succeeded; cleanup must not mask it.
            with suppress(Exception):
                path.unlink(missing_ok=True)


def get_backend(
    directory: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")
) -> StorageBackend:
    """Return a cached safe backend, verifying gcsfuse mappings before first use."""
    resolved = directory.resolve()
    with _BACKENDS_LOCK:
        # The lock ensures only one process thread performs the mapping probe.
        cached = _BACKENDS.get(resolved)
        if cached is not None:
            return cached
        if resolved in _BACKEND_ERRORS:
            # Cache only the sanitized message, never provider or bucket contents.
            raise LeaseUnavailableError(_BACKEND_ERRORS[resolved])
        mount = detect_gcs_mount(resolved, mountinfo_path)
        if mount is None:
            # The web service has no process-local coordination mode.
            message = "Experiment storage is not backed by the required Cloud Storage volume."
            _BACKEND_ERRORS[resolved] = message
            raise LeaseUnavailableError(message)
        try:
            # ADC authorizes API fencing for the bucket discovered from mountinfo.
            client = _new_client()
            bucket = client.bucket(mount.bucket_name)
            _verify_mapping(resolved, mount, bucket)
        except Exception as exc:
            message = "Cloud Storage mount verification failed."
            # Client and probe failures may be transient, so a later request must retry.
            # In particular, do not add this operational failure to ``_BACKEND_ERRORS``.
            raise LeaseUnavailableError(message) from exc
        backend = StorageBackend(resolved, mount, bucket)
        _BACKENDS[resolved] = backend
        return backend


def reset_backend_cache() -> None:
    """Clear process detection state for isolated tests that change mount fixtures."""
    # Production never calls this helper; it deliberately does not alter credentials.
    with _BACKENDS_LOCK:
        _BACKENDS.clear()
        # Tests can then exercise a corrected deployment after a cached failure.
        _BACKEND_ERRORS.clear()
