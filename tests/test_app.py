# Import the flask app safely
import importlib.util
import json
import sys
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location("flask_app", "flask-app.py")
flask_app_module = importlib.util.module_from_spec(spec)
sys.modules["flask_app"] = flask_app_module
spec.loader.exec_module(flask_app_module)
app = flask_app_module.app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_index(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Jim Salsman" in html
    assert "templates/" not in html


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_leaderboard_route_no_data(client, tmp_path):
    # Monkeypatch RECORDS_FILE to a non-existent file
    with patch.object(flask_app_module, "RECORDS_FILE", str(tmp_path / "does_not_exist.jsonl")):
        response = client.get("/api/leaderboard")
        assert response.status_code == 200  # the html handles empty data
        html = response.data.decode("utf-8")
        assert "No complete data" in html or "No complete 2x2 cells" in html


@patch("flask_app.run_session_generator")
def test_experiment_stream(mock_generator, client):
    # Mock the generator to yield a start, status, and record event
    def mock_run(*args, **kwargs):
        yield {
            "type": "turn_complete",
            "model": "test",
            "variant": "bare",
            "turn": 1,
            "turn_time": 1.0,
        }
        yield {
            "type": "record",
            "record": {
                "session": 1,
                "model": "test",
                "variant": "bare",
                "depth": 1,
                "score": 100,
                "stats": {},
                "judge_overall": 1,
                "reply": "test",
                "timestamp": "2023-01-01T00:00:00Z",
            },
        }

    mock_generator.side_effect = mock_run

    # Send valid request
    response = client.post(
        "/api/experiment/stream",
        json={"api_key": "test_key", "model": "anthropic/claude-sonnet-4.5"},
    )

    assert response.status_code == 200
    assert response.mimetype == "application/x-ndjson"

    # Read the streamed lines
    data = response.data.decode("utf-8")
    lines = [line for line in data.split("\n") if line.strip()]

    assert len(lines) > 0
    parsed_lines = [json.loads(line) for line in lines]

    # Assert NDJSON structure and events
    assert parsed_lines[0]["type"] == "start"
    assert any(event["type"] == "status" for event in parsed_lines)
    assert parsed_lines[-1]["type"] == "success"

    # Ensure ETA is handled properly in status
    status_events = [e for e in parsed_lines if e["type"] == "status"]
    if status_events:
        assert "eta" in status_events[0]


def test_experiment_stream_missing_key(client):
    response = client.post("/api/experiment/stream", json={"model": "anthropic/claude-sonnet-4.5"})
    assert response.status_code == 401
    assert "Missing OpenRouter API Key" in response.json["error"]


def test_experiment_stream_invalid_model(client):
    response = client.post(
        "/api/experiment/stream", json={"api_key": "test_key", "model": "invalid-model"}
    )
    assert response.status_code == 400
    assert "Unsupported model" in response.json["error"]
