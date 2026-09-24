"""Transport-independent checks for resumable experiment orchestration."""

from ste.experiment import INTERACTIVE_DEADLINE_SECONDS, MAX_WORK_UNITS, run_experiment


def test_resume_skips_saved_unit_and_rebuilds_context():
    """Avoid a repeated paid call while retaining the saved conversation answer."""
    saved = {
        "session": 1,
        "model": "openai/gpt-6-sol",
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
            "openai/gpt-6-sol",
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
    # A resumed legacy arm and every newly generated arm retain one run identity.
    assert {record["run_id"] for record in checkpoints[-1]} == {"a" * 32}
    # Normalizing the checkpoint must not alter the caller's in-memory snapshot.
    assert "run_id" not in saved
    assert events[0]["completed"] == 1 and events[-1]["type"] == "success"
    assert all("sample-secret" not in str(event) for event in events)


def test_maximum_run_has_a_request_timeout_below_the_overall_deadline():
    """Keep the maximum serial workload within the synchronous deployment budget."""
    timeouts = []

    def request(_key, _model, _messages, **options):
        """Record the bounded timeout while replacing every paid provider call."""
        timeouts.append(options["timeout"])
        # The deterministic reply is scored locally without external services.
        return "Use a short active sentence."

    events = list(run_experiment("secret", "openai/gpt-6-sol", 1, 3, request=request))

    assert events[-1]["type"] == "success"
    assert len(timeouts) == MAX_WORK_UNITS
    assert sum(timeouts) < INTERACTIVE_DEADLINE_SECONDS
