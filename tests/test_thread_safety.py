"""Native-thread stress checks for the mutable filesystem coordination state."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ste.runs.lease import RunActiveError, acquire_lease
from ste.runs.store import create_run, save_run


def test_same_run_lease_has_one_native_thread_owner(tmp_path):
    """Allow exactly one of five simultaneous native threads to own a run lease."""
    barrier = threading.Barrier(5)

    def contend(index):
        """Attempt one lease after every executor thread reaches the barrier."""
        barrier.wait()
        try:
            lease = acquire_lease("d" * 32, tmp_path)
        except RunActiveError:
            # A losing thread must stop before it could start duplicate paid work.
            return False
        time.sleep(0.1)
        lease.release()
        # Holding the descriptor briefly gives all other native threads time to contend.
        return index >= 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(contend, range(5)))
    assert results.count(True) == 1


def test_snapshot_replacement_is_serialized_across_native_threads(tmp_path):
    """Keep a shared run snapshot valid while multiple native threads update it."""
    state = create_run(tmp_path, "openai/gpt-4o", 1, 1)

    def write_snapshot(index):
        """Write one complete independent state through the shared per-run lock."""
        snapshot = dict(state)
        # Each thread owns its nested list, so only the filesystem target is shared.
        snapshot["records"] = [{"writer": index}]
        save_run(tmp_path, snapshot)

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(write_snapshot, range(20)))
    stored = json.loads((tmp_path / f"{state['run_id']}.json").read_text())
    # A serialized winner must leave one intact JSON document, never interleaved output.
    assert stored["records"][0]["writer"] in range(20)
