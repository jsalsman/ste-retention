"""HTTP and static-contract tests with every paid inference path mocked."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def module(tmp_path):
    """Import the required hyphenated Flask entry point without normal module syntax."""
    spec = importlib.util.spec_from_file_location("flask_app", ROOT / "flask-app.py")
    loaded = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(loaded)
    loaded.app.config.update(TESTING=True)
    # Every test gets an isolated stand-in for the production /experiments mount.
    loaded.EXPERIMENTS = tmp_path / "experiments"
    return loaded


@pytest.fixture()
def client(module):
    """Return an isolated Flask test client."""
    return module.app.test_client()


def test_public_routes_and_assets(client):
    """Serve the standalone page, health probe, assets, and method errors."""
    assert b"Jim Salsman" in client.get("/").data
    assert client.get("/healthz").json == {"status": "ok"}
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/loading.gif").status_code == 200
    assert client.post("/").status_code == 405


def test_static_page_contract():
    """Require accessibility hooks and standalone assets without Jinja or a bar."""
    page = (ROOT / "index.html").read_text()
    assert "Jim Salsman" in page
    assert all(
        asset in page for asset in ("static/styles.css", "static/app.js", "static/loading.gif")
    )
    assert 'aria-live="polite"' in page
    overlay = page.split('id="loading-overlay"', 1)[1]
    assert 'id="cancel"' in overlay and page.count('id="cancel"') == 1
    assert "{{" not in page and "progress" not in page.lower()
    assert not (ROOT / "templates").exists()


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
    assert b"Rule effect" in response.data and b"+10.0" in response.data
    assert b"Naming effect" in response.data and b"+20.0" in response.data


def test_interaction_validation_and_mocked_success(client, module, monkeypatch):
    """Reject invalid inputs and return only mocked model output on success."""
    assert client.post("/api/interact", json={}).status_code == 400
    monkeypatch.setattr(module, "chat", lambda *_args, **_kwargs: "Safe answer")
    response = client.post(
        "/api/interact",
        json={"api_key": "sample-secret", "model": "openai/gpt-4o", "prompt": "Hello"},
    )
    assert response.json == {"answer": "Safe answer"}
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
        json={"api_key": "sample-secret", "model": "openai/gpt-4o", "batches": 1, "turns": 1},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["status", "status", "success"]
    assert events[1]["eta_seconds"] == 2
    assert len(events[0]["run_id"]) == 32
    assert "sample-secret" not in response.text


def test_interrupted_run_is_persisted_and_can_resume(client, module, monkeypatch):
    """Checkpoint partial work and supply it to a later request with the same run ID."""

    def interrupted(*_args, persist, run_id, **_kwargs):
        record = {
            "session": 1,
            "model": "openai/gpt-4o",
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
    request_data = {"api_key": "sample-secret", "model": "openai/gpt-4o", "batches": 1, "turns": 1}
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


def test_run_status_distinguishes_active_stalled_and_complete(client, module):
    """Derive liveness from durable heartbeat expiry without exposing record text."""
    state = module.create_run(module.EXPERIMENTS, "openai/gpt-4o", 1, 1)
    state["status"] = "running"
    state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    state["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    module.save_run(module.EXPERIMENTS, state)
    endpoint = f"/api/experiments/{state['run_id']}/status"
    assert client.get(endpoint).json["liveness"] == "active"

    # An expired heartbeat means no worker has renewed the run lease in time.
    state["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    module.save_run(module.EXPERIMENTS, state)
    assert client.get(endpoint).json["liveness"] == "stalled"
    state["status"] = "complete"
    state["lease_expires_at"] = None
    module.save_run(module.EXPERIMENTS, state)
    response = client.get(endpoint)
    assert response.json["liveness"] == "complete" and "records" not in response.json


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
        json={"api_key": "sample-secret", "model": "openai/gpt-4o", "batches": 1, "turns": 1},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "error"
    assert "sample-secret" not in response.text and "Authorization" not in response.text


def test_workload_limits(client):
    """Reject excess work before producing a streaming response."""
    response = client.post(
        "/api/experiments/stream",
        json={"api_key": "x", "model": "openai/gpt-4o", "batches": 3, "turns": 1},
    )
    assert response.status_code == 400
    assert response.content_type == "application/json"
