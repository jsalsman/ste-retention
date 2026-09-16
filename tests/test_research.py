"""Research-protocol, scoring, resume, and authorization regression tests."""

import importlib.util
import json
from pathlib import Path

import pytest

from ste.protocol import DEFAULT_RESEARCH_DEPTHS, PROMPT_POOL, STE_RULES, VARIANTS
from ste.research import (
    ResearchConfig,
    load_state,
    new_state,
    prompt_sequence,
    run_research,
    save_state,
)
from ste.runs.store import completed_records
from ste.scoring import parse_judge_score, score_text

ROOT = Path(__file__).parents[1]


def test_full_protocol_and_deterministic_shared_sequence():
    """Retain the complete pool, probes, equivalent rule suffix, and stable sequences."""
    assert len(PROMPT_POOL) == 32 and DEFAULT_RESEARCH_DEPTHS == (1, 6, 12)
    # Detailed arms differ only in naming prefix, never in their rule content.
    assert VARIANTS["rules"].endswith(STE_RULES)
    assert VARIANTS["named_rules"].endswith(STE_RULES)
    assert prompt_sequence(91, "session", 12) == prompt_sequence(91, "session", 12)
    assert prompt_sequence(92, "session", 12) != prompt_sequence(91, "session", 12)
    # A session must not prime a later response by presenting the same prompt twice.
    _, prompts = prompt_sequence(91, "session", len(PROMPT_POOL))
    assert len(prompts) == len(set(prompts))


def test_research_depth_cannot_exceed_non_repeating_pool():
    """Reject protocol configurations that require reusing a prompt in one session."""
    config = ResearchConfig(("openai/gpt-4o",), 1, (len(PROMPT_POOL) + 1,))
    # Both the public configuration and lower-level sequence helper fail explicitly.
    with pytest.raises(ValueError, match="non-repeating prompt pool"):
        config.validate()
    with pytest.raises(ValueError, match="non-repeating prompt pool"):
        prompt_sequence(1, "session", len(PROMPT_POOL) + 1)


def test_completed_records_loads_completed_research_schema(tmp_path):
    """Expose completed worker snapshots without applying the preview resume schema."""
    run_id = "c" * 32
    config = ResearchConfig(("openai/gpt-4o",), 1, (1,))
    state = new_state(config, run_id)
    # This is the versioned record shape emitted by the asynchronous research worker.
    record = {
        "schema_version": state["schema_version"],
        "protocol_version": state["protocol_version"],
        "scoring_version": state["scoring_version"],
        "run_mode": "research",
        "run_id": run_id,
        "session": 1,
        "model": "openai/gpt-4o",
        "variant": "bare",
        "depth": 1,
        "score": 75.0,
    }
    state.update(status="complete", records=[record])
    save_state(tmp_path / f"{run_id}.json", state)
    # Preview-only fields such as owner_id and batches are intentionally absent.
    assert completed_records(tmp_path) == [record]


@pytest.mark.parametrize("text", ["", "```markup only```", "*** ### ---"])
def test_empty_and_markup_only_outputs_are_finite_zero_length(text):
    """Treat content-free model output as a bounded failure rather than a pass."""
    result = score_text(text)
    assert result["sentence_length"] == 0
    assert 0 <= result["score"] <= 100


def test_sentence_voice_vocabulary_and_optional_composite():
    """Expose long/short, passive/active, vocabulary, and missing optional metrics."""
    active = score_text("The pump moves water.", {"the", "pump", "moves"})
    passive = score_text("The pump was damaged.", {"the", "pump"}, 20)
    long = score_text(" ".join(["word"] * 26) + ".")
    # Missing vocabulary is unavailable; supplied vocabulary reports partial compliance.
    assert score_text("The pump moves water.")["approved_vocabulary"] is None
    assert active["active_voice"] == 1 and active["approved_vocabulary"] == pytest.approx(0.75)
    assert passive["active_voice"] == 0 and passive["judge"] == 20
    assert long["sentence_length"] == 0
    assert all(0 <= value["score"] <= 100 for value in (active, passive, long))


@pytest.mark.parametrize("raw", ["bad", "{}", '{"score": NaN}', '{"score": 101}', "[]"])
def test_malformed_judge_output_is_rejected(raw):
    """Reject explanatory, missing, non-finite, out-of-range, and non-object judge data."""
    with pytest.raises(ValueError):
        parse_judge_score(raw)


def test_research_resumes_inside_partial_arm_without_repeating_calls(tmp_path):
    """Rebuild context and skip completed paid units at the next missing turn."""
    config = ResearchConfig(("openai/gpt-4o",), 1, (1, 2), seed=7)
    state = new_state(config, "a" * 32)
    calls = []

    def request(_key, _model, messages, **_options):
        """Return deterministic replies while retaining context for inspection."""
        calls.append(messages)
        return f"Reply {len(calls)}."

    snapshots = []
    generator = run_research(
        "secret",
        config,
        state,
        lambda value: snapshots.append(json.loads(json.dumps(value))),
        request=request,
    )
    next(generator)
    # Simulate an abrupt interruption after the first durable generation unit.
    resumed = snapshots[-1]
    prior_calls = len(calls)
    events = list(run_research("secret", config, resumed, lambda _value: None, request=request))
    assert events[-1]["type"] == "success"
    assert len(calls) - prior_calls == config.workload()["generation_calls"] - 1
    assert any(message["content"] == "Reply 1." for message in calls[prior_calls])


def test_resume_rejects_changed_configuration_and_duplicate_units(tmp_path):
    """Reject changed semantics and duplicate idempotency units before paid work."""
    path = tmp_path / "run.json"
    config = ResearchConfig(("openai/gpt-4o",), 1, (1,), seed=3)
    state = new_state(config, "b" * 32)
    state["units"] = [{"unit_id": "same"}, {"unit_id": "same"}]
    save_state(path, state)
    with pytest.raises(ValueError):
        load_state(path, config, "b" * 32)
    state["units"] = []
    save_state(path, state)
    changed = ResearchConfig(("openai/gpt-4o",), 1, (1,), seed=4)
    with pytest.raises(ValueError):
        load_state(path, changed, "b" * 32)


def test_owner_cannot_view_resume_or_delete_another_users_run(tmp_path, monkeypatch):
    """Enforce owner checks on every run route while treating IDs as non-secret."""
    spec = importlib.util.spec_from_file_location("secured_flask_app", ROOT / "flask-app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.EXPERIMENTS = tmp_path
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUTH_TOKENS_JSON", json.dumps({"alice-token": "alice", "bob-token": "bob"}))
    state = module.create_run(tmp_path, "openai/gpt-4o", 1, 1, owner_id="alice")
    bob = {"Authorization": "Bearer bob-token"}
    endpoint = f"/api/experiments/{state['run_id']}"
    client = module.app.test_client()
    assert client.get(endpoint + "/status", headers=bob).status_code == 404
    assert client.delete(endpoint, headers=bob).status_code == 404
    response = client.post(
        "/api/experiments/stream",
        headers=bob,
        json={
            "api_key": "x",
            "model": "openai/gpt-4o",
            "batches": 1,
            "turns": 1,
            "resume_run_id": state["run_id"],
        },
    )
    assert response.status_code == 400
