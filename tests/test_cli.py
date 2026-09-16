"""Checks for durable command-line checkpoint behavior with mocked inference."""

import json

import pytest

import ste_retention


def test_cli_checkpoints_before_a_late_failure(tmp_path, monkeypatch):
    """Keep an already-paid response when a later orchestrator operation fails."""
    output = tmp_path / "records.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["ste_retention.py", "--model", "openai/gpt-4o", "--records", str(output)],
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sample-secret")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    def interrupted(*_args, persist, **_kwargs):
        """Checkpoint one safe record and then emulate a later provider timeout."""
        record = {
            "session": 1,
            "model": "openai/gpt-4o",
            "variant": "bare",
            "depth": 1,
            "score": 50,
            "text": "Paid response",
        }
        # Persistence occurs before the exception, exactly as in the orchestrator.
        persist([record])
        raise TimeoutError
        yield  # pragma: no cover - make this mock a generator like the real API

    monkeypatch.setattr(ste_retention, "run_experiment", interrupted)
    with pytest.raises(TimeoutError):
        ste_retention.main()

    saved = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(saved) == 1 and saved[0]["text"] == "Paid response"
