"""Research-protocol, scoring, and resume regression tests."""

import json

import pytest

from ste.protocol import (
    DEFAULT_RESEARCH_DEPTHS,
    PROMPT_POOL,
    PROTOCOL_VERSION,
    STE_RULES,
    VARIANTS,
)
from ste.models.openrouter import DEFAULT_MAX_TOKENS
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
    config = ResearchConfig(("openai/gpt-6-sol",), 1, (len(PROMPT_POOL) + 1,))
    # Both the public configuration and lower-level sequence helper fail explicitly.
    with pytest.raises(ValueError, match="non-repeating prompt pool"):
        config.validate()
    with pytest.raises(ValueError, match="non-repeating prompt pool"):
        prompt_sequence(1, "session", len(PROMPT_POOL) + 1)


def test_research_defaults_to_expanded_generation_token_limit():
    """Give full studies the same substantial default output capacity as previews."""
    config = ResearchConfig(("openai/gpt-6-sol",), 1, (1,))
    # The value is persisted in run configuration, making resumed behavior reproducible.
    assert config.max_tokens == DEFAULT_MAX_TOKENS == 8192
    # Validation confirms the shared default is a supported positive integer limit.
    config.validate()


@pytest.mark.parametrize("depths", [(-1, 1), (0, 1), (1, 2.5, 3), (True, 2)])
def test_research_config_validates_every_requested_depth(depths):
    """Reject invalid early and middle depths even when the final depth is valid."""
    config = ResearchConfig(("openai/gpt-6-sol",), 1, depths)
    # Validation must inspect each requested probe rather than relying on the maximum.
    # This prevents a run from silently omitting an invalid requested record.
    with pytest.raises(ValueError, match="Research depth is out of range"):
        config.validate()


def test_completed_records_loads_completed_research_schema(tmp_path):
    """Expose completed worker snapshots without applying the preview resume schema."""
    run_id = "c" * 32
    config = ResearchConfig(("openai/gpt-6-sol",), 1, (1,))
    state = new_state(config, run_id)
    # This is the versioned record shape emitted by the asynchronous research worker.
    record = {
        "schema_version": state["schema_version"],
        "protocol_version": state["protocol_version"],
        "scoring_version": state["scoring_version"],
        "run_mode": "research",
        "run_id": run_id,
        "session": 1,
        "model": "openai/gpt-6-sol",
        "variant": "bare",
        "depth": 1,
        "score": 75.0,
    }
    state.update(status="complete", records=[record])
    # Operator-selected state paths need not encode the independently generated run ID.
    save_state(tmp_path / "operator-selected-state.json", state)
    # Preview-only fields and a filename-derived run ID are intentionally unnecessary.
    assert completed_records(tmp_path) == [record]


def test_load_state_rejects_snapshot_from_repeating_prompt_protocol(tmp_path):
    """Reject paid partial results whose prompts came from the prior algorithm."""
    path = tmp_path / "legacy.json"
    run_id = "d" * 32
    config = ResearchConfig(("openai/gpt-6-sol",), 1, (1,), seed=3)
    state = new_state(config, run_id)
    # Version 2.0 sampled prompts with replacement, making its saved units unsafe to resume.
    state["protocol_version"] = "ste-retention-2.0"
    save_state(path, state)
    # The current protocol must fail before old responses can enter reconstructed history.
    assert PROTOCOL_VERSION != "ste-retention-2.0"
    with pytest.raises(ValueError, match="incompatible versions"):
        load_state(path, config, run_id)


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
    config = ResearchConfig(("openai/gpt-6-sol",), 1, (1, 2), seed=7)
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
    config = ResearchConfig(("openai/gpt-6-sol",), 1, (1,), seed=3)
    state = new_state(config, "b" * 32)
    state["units"] = [{"unit_id": "same"}, {"unit_id": "same"}]
    save_state(path, state)
    with pytest.raises(ValueError):
        load_state(path, config, "b" * 32)
    state["units"] = []
    save_state(path, state)
    changed = ResearchConfig(("openai/gpt-6-sol",), 1, (1,), seed=4)
    with pytest.raises(ValueError):
        load_state(path, changed, "b" * 32)
