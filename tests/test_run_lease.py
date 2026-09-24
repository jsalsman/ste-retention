"""Lease tests that never contact Cloud Storage or require cloud credentials."""

import pytest

from ste.runs.lease import LeaseUnavailableError, RunActiveError, acquire_lease


def test_local_lease_rejects_concurrent_owner_and_allows_release(tmp_path):
    """Serialize same-run requests while allowing ownership after explicit release."""
    run_id = "b" * 32
    first = acquire_lease(run_id, tmp_path)
    try:
        # A second user cannot repeat paid work while the first heartbeat is live.
        with pytest.raises(RunActiveError):
            acquire_lease(run_id, tmp_path)
    finally:
        first.release()
    second = acquire_lease(run_id, tmp_path)
    # Cleanup keeps the process-global development backend isolated for later tests.
    second.release()


def test_unsupported_flock_raises_lease_unavailable(tmp_path, monkeypatch):
    """Reject a local filesystem whose flock implementation is unsupported."""
    import ste.runs.lease as run_lease

    def unsupported(*_args):
        """Represent a mounted filesystem without flock support."""
        # Unsupported locking cannot provide safe ownership across containers.
        raise OSError("locking unsupported")

    monkeypatch.setattr(run_lease.fcntl, "flock", unsupported)
    # Surface a sanitized availability failure instead of claiming unsafe ownership.
    with pytest.raises(LeaseUnavailableError, match="locking is unavailable"):
        acquire_lease("c" * 32, tmp_path)
