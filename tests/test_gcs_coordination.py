"""Generation-precondition lease and snapshot tests using only in-memory storage."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ste.runs.lease as lease_module
from ste.runs.backend import GCSMount, StorageBackend
from ste.runs.lease import RunActiveError, acquire_lease
from ste.runs.store import SnapshotFencer
from tests.fake_gcs import FakeBucket


def _backend(tmp_path: Path, bucket: FakeBucket) -> StorageBackend:
    """Build a verified-style backend descriptor around an in-memory fake bucket."""
    # Unit tests bypass mount probing because probe behavior has separate coverage.
    mount = GCSMount(tmp_path, "test-bucket", "")
    return StorageBackend(tmp_path, mount, bucket)


def test_gcs_fresh_acquire_contention_heartbeat_and_release(tmp_path, monkeypatch):
    """Fence a live owner, advance its generation, and permit work after release."""
    bucket = FakeBucket()
    backend = _backend(tmp_path, bucket)
    monkeypatch.setattr(lease_module, "get_backend", lambda _directory: backend)
    first = acquire_lease("a" * 32, tmp_path)
    original_generation = first.generation
    # A live object rejects a second independently generated owner identifier.
    with pytest.raises(RunActiveError, match="still running"):
        acquire_lease("a" * 32, tmp_path)
    first.heartbeat()
    assert first.generation > original_generation
    first.release()
    second = acquire_lease("a" * 32, tmp_path)
    second.release()


def test_gcs_expired_and_malformed_leases_are_taken_over(tmp_path, monkeypatch):
    """Conditionally replace stale metadata while preserving generation ordering."""
    bucket = FakeBucket()
    backend = _backend(tmp_path, bucket)
    monkeypatch.setattr(lease_module, "get_backend", lambda _directory: backend)
    name = f"leases/{'b' * 32}.lock"
    expired = {
        "owner_id": "old",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "lease_expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }
    bucket.blob(name).upload_from_string(
        json.dumps(expired), content_type="application/json", if_generation_match=0
    )
    lease = acquire_lease("b" * 32, tmp_path)
    lease.release()
    # Empty malformed content is also stale and can be fenced by its generation.
    bucket.blob(name).upload_from_string(
        b"", content_type="application/json", if_generation_match=0
    )
    lease = acquire_lease("b" * 32, tmp_path)
    lease.release()


def test_gcs_ambiguous_heartbeat_is_adopted_and_stolen_owner_is_rejected(tmp_path, monkeypatch):
    """Adopt an ambiguous own write but stop after another owner replaces the lease."""
    bucket = FakeBucket()
    backend = _backend(tmp_path, bucket)
    monkeypatch.setattr(lease_module, "get_backend", lambda _directory: backend)
    lease = acquire_lease("c" * 32, tmp_path)
    bucket.ambiguous_uploads = 1
    lease.heartbeat()
    current = bucket.objects[lease.object_name]
    stolen = json.loads(current.contents)
    stolen["owner_id"] = "winner"
    bucket.blob(lease.object_name).upload_from_string(
        json.dumps(stolen), content_type="application/json", if_generation_match=current.generation
    )
    with pytest.raises(RunActiveError, match="ownership changed"):
        lease.heartbeat()
    # Release ignores the stale generation precondition and cannot delete the winner.
    lease.release()
    assert lease.object_name in bucket.objects


def test_snapshot_fencer_reads_generation_and_rejects_stale_writer(tmp_path):
    """Read fresh API state and prevent an old generation from overwriting new work."""
    bucket = FakeBucket()
    backend = _backend(tmp_path, bucket)
    run_id = "d" * 32
    first = SnapshotFencer(tmp_path, run_id, backend)
    state = {"run_id": run_id, "status": "running"}
    first.write(state)
    stale = SnapshotFencer(tmp_path, run_id, backend)
    assert json.loads(stale.read_bytes())["status"] == "running"
    state["status"] = "complete"
    first.write(state)
    with pytest.raises(RunActiveError, match="ownership changed"):
        stale.write({"run_id": run_id, "status": "interrupted"})
    generation = bucket.objects[f"{run_id}.json"].generation
    # A cleanup call is rejected in memory and performs no additional API mutation.
    with pytest.raises(RunActiveError):
        stale.write({"run_id": run_id, "status": "interrupted"})
    assert bucket.objects[f"{run_id}.json"].generation == generation


def test_snapshot_ambiguous_upload_adopts_new_generation(tmp_path):
    """Recognize a successful upload whose client response reported a precondition error."""
    bucket = FakeBucket()
    backend = _backend(tmp_path, bucket)
    fencer = SnapshotFencer(tmp_path, "e" * 32, backend)
    bucket.ambiguous_uploads = 1
    fencer.write({"run_id": "e" * 32, "status": "running"})
    # The adopted generation permits a subsequent genuinely conditional write.
    fencer.write({"run_id": "e" * 32, "status": "complete"})
    assert fencer.generation == bucket.objects[f"{'e' * 32}.json"].generation
