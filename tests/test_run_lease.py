"""Lease tests that never contact Cloud Storage or require cloud credentials."""

import pytest

from run_lease import RunActiveError, acquire_lease


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


def test_unsupported_flock_uses_timestamp_fallback(tmp_path, monkeypatch):
    """Use live timestamp metadata when a FUSE implementation rejects file locking."""
    import run_lease

    def unsupported(*_args):
        """Represent a mounted filesystem without flock support."""
        # The production code must recover rather than failing the whole experiment.
        raise OSError("locking unsupported")

    monkeypatch.setattr(run_lease.fcntl, "flock", unsupported)
    first = acquire_lease("c" * 32, tmp_path)
    try:
        # A recent fallback heartbeat still rejects a second same-run request.
        with pytest.raises(RunActiveError):
            acquire_lease("c" * 32, tmp_path)
        first.heartbeat()
    finally:
        first.release()
