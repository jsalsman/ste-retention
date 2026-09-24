"""HTTP and static-contract tests with every paid inference path mocked."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ste.runs.lease as lease_module
from ste.models.openrouter import ChatCompletion, IncompleteGenerationError
from ste.protocol import ALLOWED_MODELS
from ste.runs.backend import GCSMount, StorageBackend
from ste.runs.store import SnapshotFencer
from tests.fake_gcs import FakeBucket

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def module(tmp_path, monkeypatch):
    """Import the required hyphenated Flask entry point without normal module syntax."""
    spec = importlib.util.spec_from_file_location("flask_app", ROOT / "flask-app.py")
    loaded = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(loaded)
    loaded.app.config.update(TESTING=True)
    # Every test gets an isolated stand-in for the production /experiments mount.
    loaded.EXPERIMENTS = tmp_path / "experiments"
    bucket = FakeBucket(loaded.EXPERIMENTS)
    backend = StorageBackend(
        loaded.EXPERIMENTS,
        GCSMount(loaded.EXPERIMENTS, "test-bucket", ""),
        bucket,
    )
    loaded.TEST_BUCKET = bucket
    loaded.STORAGE_BACKEND = backend
    # Web tests exercise API-only coordination without credentials or a real mount.
    monkeypatch.setattr(lease_module, "get_backend", lambda _directory: backend)
    return loaded


@pytest.fixture()
def client(module):
    """Return an isolated Flask test client."""
    return module.app.test_client()


def _write_snapshot(module, state):
    """Conditionally persist app-test state through the in-memory GCS backend."""
    fencer = SnapshotFencer(module.EXPERIMENTS, state["run_id"], module.STORAGE_BACKEND)
    # Existing snapshots must be observed before a generation-conditioned update.
    if f"{state['run_id']}.json" in module.TEST_BUCKET.objects:
        fencer.read_bytes()
    fencer.write(state)


def test_public_routes_and_assets(client):
    """Serve the standalone page, health probe, assets, and method errors."""
    assert b"Jim Salsman" in client.get("/").data
    assert client.get("/api/healthz").json == {"status": "ok"}
    assert client.get("/healthz").status_code == 404
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/loading.gif").status_code == 200
    assert client.post("/").status_code == 405


def test_static_page_contract():
    """Require accessibility hooks and standalone assets without Jinja or a bar."""
    page = (ROOT / "index.html").read_text()
    assert "Jim Salsman" in page
    # Header credits must lead directly to the author's profile and project source.
    assert '<a href="https://linkedin.com/in/jsalsman">Jim Salsman</a>' in page
    assert '<a href="https://github.com/jsalsman/ste-retention">STE Retention Lab</a>' in page
    # Every server-approved model must be exposed by the standalone form.
    assert all(f'value="{model}"' in page for model in ALLOWED_MODELS)
    # Exactly four options keep the client and the explicit server allow-list aligned.
    assert page.count("<option value=") == len(ALLOWED_MODELS) + 2
    assert all(
        asset in page for asset in ("static/styles.css", "static/app.js", "static/loading.gif")
    )
    assert 'aria-live="polite"' in page
    overlay = page.split('id="loading-overlay"', 1)[1]
    assert 'id="cancel"' in overlay and page.count('id="cancel"') == 1
    assert "{{" not in page and "progress" not in page.lower()
    assert not (ROOT / "templates").exists()

    # Request cleanup must restore disabled state for hidden required mode fields.
    script = (ROOT / "static" / "app.js").read_text()
    cleanup = script.split("function setRunning", 1)[1].split("async function askModel", 1)[0]
    assert "showMode();" in cleanup
    # Interaction output stays inert and explicitly distinguishes partial generations.
    assert "result.textContent = data.answer" in script
    assert "displayed text may be truncated" in script
    assert "result.innerHTML" not in script


def test_leaderboard_missing_and_available(client, module, tmp_path, monkeypatch):
    """Give a helpful absence and escape external model names when data exists."""
    monkeypatch.setattr(module, "RECORDS", tmp_path / "missing.jsonl")
    assert client.get("/leaderboard").status_code == 404
    path = tmp_path / "records.jsonl"
    records = [
        {"session": 1, "model": "<x>", "variant": variant, "depth": 1, "score": score}
        for variant, score in (("bare", 50), ("rules", 60), ("named", 70), ("named_rules", 90))
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setattr(module, "RECORDS", path)
    response = client.get("/leaderboard")
    assert response.status_code == 200
    assert b"&lt;x&gt;" in response.data and b"<x>" not in response.data
    assert b"Rule effect" in response.data and b"+15.0" in response.data
    assert b"Naming effect" in response.data and b"+25.0" in response.data


def test_interaction_validation_and_mocked_success(client, module, monkeypatch):
    """Reject invalid inputs and return only mocked model output on success."""
    assert client.post("/api/interact", json={}).status_code == 400
    monkeypatch.setattr(
        module,
        "chat",
        lambda *_args, **_kwargs: ChatCompletion("Safe answer", "stop", True),
    )
    response = client.post(
        "/api/interact",
        json={"api_key": "sample-secret", "model": "openai/gpt-6-sol", "prompt": "Hello"},
    )
    assert response.json == {
        "answer": "Safe answer",
        "complete": True,
        "finish_reason": "stop",
    }
    assert b"sample-secret" not in response.data and b"Authorization" not in response.data


def test_interaction_returns_safe_partial_text_for_truncation(client, module, monkeypatch):
    """Translate explicit provider truncation without leaking credentials or bodies."""

    def truncated(*_args, **_kwargs):
        """Stand in for a provider that returned useful text before its token limit."""
        raise IncompleteGenerationError("Partial <strong>model text</strong>")

    monkeypatch.setattr(module, "chat", truncated)
    response = client.post(
        "/api/interact",
        json={"api_key": "sample-secret", "model": "openai/gpt-6-sol", "prompt": "Hello"},
    )
    assert response.status_code == 200
    assert response.json == {
        "answer": "Partial <strong>model text</strong>",
        "complete": False,
        "finish_reason": "length",
    }
    assert b"sample-secret" not in response.data and b"Authorization" not in response.data


def test_stream_framing_status_eta_and_terminal(client, module, monkeypatch):
    """Return independently valid NDJSON with statuses, ETA, and one terminal event."""

    def fake(*_args, **_kwargs):
        yield {
            "type": "status",
            "message": "start",
            "completed": 0,
            "total": 4,
            "elapsed_seconds": 0,
        }
        yield {
            "type": "status",
            "message": "work",
            "completed": 2,
            "total": 4,
            "elapsed_seconds": 2,
            "eta_seconds": 2,
        }
        yield {
            "type": "success",
            "message": "done",
            "completed": 4,
            "total": 4,
            "elapsed_seconds": 4,
        }

    monkeypatch.setattr(module, "run_experiment", fake)
    response = client.post(
        "/api/experiments/stream",
        json={"api_key": "sample-secret", "model": "openai/gpt-6-sol", "batches": 1, "turns": 1},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["status", "status", "success"]
    assert events[1]["eta_seconds"] == 2
    assert len(events[0]["run_id"]) == 32
    assert "sample-secret" not in response.text


def test_full_research_stream_is_checkpointed_and_resumable(client, module, monkeypatch):
    """Use the research orchestrator and resume its saved full-study snapshot."""

    def fake_research(_key, config, state, persist):
        """Simulate one paid unit and the terminal worker event."""
        if not state["units"]:
            # A resumed call must keep the durable unit and must not repeat paid work.
            state["units"].append({"unit_id": "unit-1", "response": "Saved"})
            persist(state)
            yield {"type": "unit", "unit_id": "unit-1", "kind": "generation"}
        state["status"] = "complete"
        persist(state)
        yield {"type": "success", "run_id": state["run_id"], "records": 0}

    monkeypatch.setattr(module, "run_research", fake_research)
    request_data = {
        "api_key": "sample-secret",
        "run_mode": "research",
        "model": "openai/gpt-6-sol",
        "sessions": 1,
    }
    response = client.post("/api/experiments/stream", json=request_data)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["status", "status", "success"]
    run_id = events[0]["run_id"]
    saved = json.loads((module.EXPERIMENTS / f"{run_id}.json").read_text())
    assert saved["run_mode"] == "research" and saved["status"] == "complete"
    assert "sample-secret" not in response.text and "api_key" not in saved

    # The public status route understands the research snapshot and its call count.
    status = client.get(f"/api/experiments/{run_id}/status")
    assert status.json["liveness"] == "complete"
    assert status.json["completed"] == 1 and status.json["total"] == 48

    request_data["resume_run_id"] = run_id
    resumed = client.post("/api/experiments/stream", json=request_data)
    resumed_events = [json.loads(line) for line in resumed.text.splitlines()]
    assert [event["type"] for event in resumed_events] == ["status", "success"]
    assert resumed_events[0]["completed"] == 1


def test_interrupted_run_is_persisted_and_can_resume(client, module, monkeypatch):
    """Checkpoint partial work and supply it to a later request with the same run ID."""

    def interrupted(*_args, persist, run_id, **_kwargs):
        record = {
            "session": 1,
            "model": "openai/gpt-6-sol",
            "variant": "bare",
            "depth": 1,
            "score": 50,
            "text": "Saved response",
        }
        yield {
            "type": "status",
            "message": "start",
            "completed": 0,
            "total": 4,
            "elapsed_seconds": 0,
            "run_id": run_id,
        }
        persist([record])
        raise TimeoutError

    monkeypatch.setattr(module, "run_experiment", interrupted)
    request_data = {
        "api_key": "sample-secret",
        "model": "openai/gpt-6-sol",
        "batches": 1,
        "turns": 1,
    }
    first = client.post("/api/experiments/stream", json=request_data)
    first_events = [json.loads(line) for line in first.text.splitlines()]
    run_id = first_events[0]["run_id"]
    saved = json.loads((module.EXPERIMENTS / f"{run_id}.json").read_text())
    assert saved["status"] == "interrupted" and len(saved["records"]) == 1

    def resumed(*_args, existing_records, run_id, **_kwargs):
        assert len(existing_records) == 1
        yield {
            "type": "success",
            "message": "done",
            "completed": 1,
            "total": 4,
            "elapsed_seconds": 0,
            "records": existing_records,
            "run_id": run_id,
        }

    monkeypatch.setattr(module, "run_experiment", resumed)
    request_data["resume_run_id"] = run_id
    second = client.post("/api/experiments/stream", json=request_data)
    assert json.loads(second.text)["type"] == "success"
    saved = json.loads((module.EXPERIMENTS / f"{run_id}.json").read_text())
    assert saved["status"] == "complete"


def test_lost_lease_cannot_write_interrupted_cleanup_snapshot(client, module, monkeypatch):
    """Reject cleanup writes after a successor takes over an expired request lease."""
    observed = {}

    def stolen_lease(*_args, run_id, **_kwargs):
        """Replace lease ownership before failing the simulated experiment worker."""
        snapshot_name = f"{run_id}.json"
        lease_name = f"leases/{run_id}.lock"
        observed["snapshot_generation"] = module.TEST_BUCKET.objects[snapshot_name].generation
        current = module.TEST_BUCKET.objects[lease_name]
        winner = {
            "owner_id": "successor-owner",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        }
        # This models takeover before the successor's first snapshot checkpoint.
        module.TEST_BUCKET.blob(lease_name).upload_from_string(
            json.dumps(winner),
            content_type="application/json",
            if_generation_match=current.generation,
        )
        # The stale worker then enters its exception cleanup path.
        raise TimeoutError
        yield  # pragma: no cover - keeps this injected worker a generator

    monkeypatch.setattr(module, "run_experiment", stolen_lease)
    response = client.post(
        "/api/experiments/stream",
        json={
            "api_key": "sample-secret",
            "model": "openai/gpt-6-sol",
            "batches": 1,
            "turns": 1,
        },
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    run_id = events[-1]["run_id"]
    stored = json.loads(module.TEST_BUCKET.objects[f"{run_id}.json"].contents)
    # Heartbeat rejection prevents the stale request from writing interrupted state.
    assert stored["status"] == "running"
    assert (
        module.TEST_BUCKET.objects[f"{run_id}.json"].generation == observed["snapshot_generation"]
    )
    # Stale release is also fenced and leaves the successor's lease intact.
    lease = json.loads(module.TEST_BUCKET.objects[f"leases/{run_id}.lock"].contents)
    assert lease["owner_id"] == "successor-owner"


def test_run_status_distinguishes_active_stalled_and_complete(client, module):
    """Derive liveness from durable heartbeat expiry without exposing record text."""
    state = module.create_run("openai/gpt-6-sol", 1, 1)
    state["status"] = "running"
    state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    state["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    _write_snapshot(module, state)
    endpoint = f"/api/experiments/{state['run_id']}/status"
    assert client.get(endpoint).json["liveness"] == "active"

    # An expired heartbeat means no worker has renewed the run lease in time.
    state["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    _write_snapshot(module, state)
    assert client.get(endpoint).json["liveness"] == "stalled"
    state["status"] = "complete"
    state["lease_expires_at"] = None
    _write_snapshot(module, state)
    response = client.get(endpoint)
    assert response.json["liveness"] == "complete" and "records" not in response.json


def test_delete_rejects_active_run_then_removes_idle_snapshot(client, module):
    """Keep an active worker snapshot, then delete it after its lease is released."""
    state = module.create_run("openai/gpt-6-sol", 1, 1)
    _write_snapshot(module, state)
    lease = module.acquire_lease(state["run_id"], module.EXPERIMENTS)
    endpoint = f"/api/experiments/{state['run_id']}"
    try:
        # A concurrent delete must not let the worker recreate a reportedly deleted run.
        response = client.delete(endpoint)
        assert response.status_code == 409
        assert (module.EXPERIMENTS / f"{state['run_id']}.json").is_file()
    finally:
        lease.release()

    # Once idle, deletion owns the same lease for the complete unlink operation.
    assert client.delete(endpoint).status_code == 204
    assert not (module.EXPERIMENTS / f"{state['run_id']}.json").exists()


def test_missing_gcsfuse_returns_service_unavailable(client, module, monkeypatch):
    """Map missing required storage detection to a sanitized HTTP 503 response."""
    from ste.runs.backend import LeaseUnavailableError

    def unavailable(_directory):
        """Represent an environment without the mandatory gcsfuse mount."""
        # The production detector supplies the same safe operational message.
        raise LeaseUnavailableError(
            "Experiment storage is not backed by the required Cloud Storage volume."
        )

    monkeypatch.setattr(lease_module, "get_backend", unavailable)
    request_data = {
        "api_key": "sample-secret",
        "model": "openai/gpt-6-sol",
        "batches": 1,
        "turns": 1,
    }
    # Missing storage is unsafe in every environment supported by the web service.
    response = client.post("/api/experiments/stream", json=request_data)
    assert response.status_code == 503
    assert "required Cloud Storage volume" in response.json["error"]
    assert b"sample-secret" not in response.data


def test_preview_resume_releases_lease_after_unwrapped_snapshot_read_error(
    client, module, monkeypatch
):
    """Release preview ownership when a provider read error escapes validation handlers."""
    run_id = "8" * 32

    def failed_read(_fencer):
        """Represent a transient provider exception from the authoritative API read."""
        # This error deliberately falls outside the route's sanitized validation types.
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(SnapshotFencer, "load_run", failed_read)
    request_data = {
        "api_key": "sample-secret",
        "model": "openai/gpt-6-sol",
        "batches": 1,
        "turns": 1,
        "resume_run_id": run_id,
    }
    # Flask testing mode re-raises the exception after the route releases ownership.
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        client.post("/api/experiments/stream", json=request_data)
    assert f"leases/{run_id}.lock" not in module.TEST_BUCKET.objects


@pytest.mark.parametrize(
    "failure", [TimeoutError(), RuntimeError("Authorization: Bearer sample-secret")]
)
def test_streamed_failures_are_sanitized(client, module, monkeypatch, failure):
    """Convert exceptions after streaming starts into one sanitized terminal error."""

    def fake(*_args, **_kwargs):
        yield {
            "type": "status",
            "message": "start",
            "completed": 0,
            "total": 4,
            "elapsed_seconds": 0,
        }
        raise failure

    monkeypatch.setattr(module, "run_experiment", fake)
    response = client.post(
        "/api/experiments/stream",
        json={"api_key": "sample-secret", "model": "openai/gpt-6-sol", "batches": 1, "turns": 1},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "error"
    assert "sample-secret" not in response.text and "Authorization" not in response.text


def test_workload_limits(client):
    """Reject excess work before producing a streaming response."""
    response = client.post(
        "/api/experiments/stream",
        json={"api_key": "x", "model": "openai/gpt-6-sol", "batches": 3, "turns": 1},
    )
    assert response.status_code == 400
    assert response.content_type == "application/json"
