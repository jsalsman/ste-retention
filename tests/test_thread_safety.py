"""Native-thread stress checks for the mutable filesystem coordination state."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ste.runs.lease as lease_module
from ste.runs.backend import GCSMount, StorageBackend
from ste.runs.lease import RunActiveError, acquire_lease
from tests.fake_gcs import FakeBucket


def test_same_gcs_run_has_one_cross_instance_owner(tmp_path, monkeypatch):
    """Allow one simulated instance to win through atomic bucket preconditions."""
    bucket = FakeBucket()
    backend = StorageBackend(tmp_path, GCSMount(tmp_path, "bucket", ""), bucket)
    # Every contender creates its own lease object; only the fake bucket is shared.
    monkeypatch.setattr(lease_module, "get_backend", lambda _directory: backend)
    barrier = threading.Barrier(5)

    def contend(index):
        """Act as a separate instance after every contender reaches the barrier."""
        barrier.wait()
        try:
            lease = acquire_lease("d" * 32, tmp_path)
        except RunActiveError:
            # A losing thread must stop before it could start duplicate paid work.
            return False
        time.sleep(0.1)
        lease.release()
        # Holding the object briefly gives every independent contender time to race.
        return index >= 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(contend, range(5)))
    assert results.count(True) == 1
