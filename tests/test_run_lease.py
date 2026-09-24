"""Public lease-interface tests that use no network or cloud credentials."""

import pytest

import ste.runs.lease as run_lease
from ste.runs.backend import GCSMount, LeaseUnavailableError, StorageBackend
from ste.runs.lease import RunActiveError, acquire_lease
from tests.fake_gcs import FakeBucket


def test_gcs_lease_rejects_concurrent_owner_and_allows_release(tmp_path, monkeypatch):
    """Serialize same-run requests while allowing ownership after explicit release."""
    bucket = FakeBucket()
    backend = StorageBackend(tmp_path, GCSMount(tmp_path, "bucket", ""), bucket)
    # Injecting the verified backend keeps this public-interface test offline.
    monkeypatch.setattr(run_lease, "get_backend", lambda _directory: backend)
    run_id = "b" * 32
    first = acquire_lease(run_id, tmp_path)
    try:
        # A second request cannot repeat paid work while the first lease is live.
        with pytest.raises(RunActiveError):
            acquire_lease(run_id, tmp_path)
    finally:
        first.release()
    second = acquire_lease(run_id, tmp_path)
    # Conditional deletion makes the lease available for a later request.
    second.release()


def test_missing_gcs_mount_raises_lease_unavailable(tmp_path, monkeypatch):
    """Refuse lease work when mandatory Cloud Storage mapping is unavailable."""

    def unavailable(_directory):
        """Represent backend detection without the required gcsfuse mount."""
        # No process-local fallback is supported by the web coordination layer.
        raise LeaseUnavailableError("Cloud Storage volume is unavailable.")

    monkeypatch.setattr(run_lease, "get_backend", unavailable)
    # Surface a sanitized availability failure rather than unsafe ownership.
    with pytest.raises(LeaseUnavailableError, match="volume is unavailable"):
        acquire_lease("c" * 32, tmp_path)
