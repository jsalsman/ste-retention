"""Transport-independent checks for resumable experiment orchestration."""

from experiment import run_experiment


def test_resume_skips_saved_unit_and_rebuilds_context():
    """Avoid a repeated paid call while retaining the saved conversation answer."""
    saved = {
        "session": 1,
        "model": "openai/gpt-4o",
        "variant": "bare",
        "depth": 1,
        "score": 50,
        "text": "Previously saved answer",
    }
    requests = []
    checkpoints = []

    def request(_key, _model, messages, **_options):
        """Capture safe messages and return a deterministic mock response."""
        requests.append(messages)
        # No external inference or credential content reaches the test result.
        return "New mock answer."

    events = list(
        run_experiment(
            "sample-secret",
            "openai/gpt-4o",
            1,
            2,
            request=request,
            existing_records=[saved],
            persist=lambda records: checkpoints.append(list(records)),
            run_id="a" * 32,
        )
    )

    # Eight total units minus the durable first unit requires only seven new calls.
    assert len(requests) == 7
    assert any(message["content"] == "Previously saved answer" for message in requests[0])
    assert len(checkpoints) == 7 and len(checkpoints[-1]) == 8
    assert events[0]["completed"] == 1 and events[-1]["type"] == "success"
    assert all("sample-secret" not in str(event) for event in events)
