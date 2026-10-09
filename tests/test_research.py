"""Research-protocol, scoring, and resume regression tests."""

import json

import pytest

from ste.models.openrouter import DEFAULT_MAX_TOKENS, IncompleteGenerationError
from ste.protocol import (
    DEFAULT_RESEARCH_DEPTHS,
    PROMPT_POOL,
    PROTOCOL_VERSION,
    STE_RULES,
    VARIANTS,
)
from ste.research import (
    MAX_RESEARCH_CALLS,
    MAX_RESEARCH_DEPTH,
    MAX_RESEARCH_SESSIONS,
    MAX_REQUESTS_PER_UNIT,
    MAX_WEB_RESEARCH_SESSIONS,
    ResearchConfig,
    load_state,
    new_state,
    parse_state,
    prompt_sequence,
    run_research,
    save_state,
)
from ste.costs import CHARS_PER_TOKEN, estimate_run_cost
from ste.models.catalog import MODELS_BY_ID
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
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (len(PROMPT_POOL) + 1,))
    # Both the public configuration and lower-level sequence helper fail explicitly.
    with pytest.raises(ValueError, match="non-repeating prompt pool"):
        config.validate()
    with pytest.raises(ValueError, match="non-repeating prompt pool"):
        prompt_sequence(1, "session", len(PROMPT_POOL) + 1)


def test_research_defaults_to_expanded_generation_token_limit():
    """Give full studies the same substantial default output capacity as previews."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,))
    # The value is persisted in run configuration, making resumed behavior reproducible.
    assert config.max_tokens == DEFAULT_MAX_TOKENS == 8192
    # Validation confirms the shared default is a supported positive integer limit.
    config.validate()


def test_workload_reports_and_enforces_maximum_provider_requests():
    """Count every bounded continuation in paid confirmation and safety limits."""
    config = ResearchConfig(
        ("openai/gpt-5.6-luna",), 1, (1,), judge_model="anthropic/claude-haiku-4.5"
    )
    workload = config.workload()
    # Four variants create four generation and four judge checkpoint units.
    assert workload["generation_units"] == workload["judge_units"] == 4
    # Each unit can consume the initial call plus every bounded continuation.
    assert workload["maximum_provider_requests"] == 8 * MAX_REQUESTS_PER_UNIT

    excessive = ResearchConfig(("openai/gpt-5.6-luna",), MAX_RESEARCH_SESSIONS, (32,))
    assert excessive.workload()["maximum_provider_requests"] > MAX_RESEARCH_CALLS
    # Validation applies the ceiling to paid requests rather than logical checkpoints.
    with pytest.raises(ValueError, match="paid-request safety limit"):
        excessive.validate()

    web_limit = ResearchConfig(("openai/gpt-5.6-luna",), MAX_WEB_RESEARCH_SESSIONS, (1, 6, 12))
    # The browser maximum is accepted, but its immediate successor exceeds session policy.
    web_limit.validate()
    assert web_limit.workload()["maximum_provider_requests"] <= MAX_RESEARCH_CALLS
    above_web_limit = ResearchConfig(
        ("openai/gpt-5.6-luna",), MAX_WEB_RESEARCH_SESSIONS + 1, (1, 6, 12)
    )
    with pytest.raises(ValueError, match="session count"):
        above_web_limit.validate()


def test_research_limits_bound_sessions_depth_and_paid_requests():
    """Keep public research maxima aligned with the prompt pool and practical spend."""
    # Depth cannot exceed the protocol's non-repeating source material.
    assert MAX_RESEARCH_DEPTH == len(PROMPT_POOL) == 32
    # Session and paid-request ceilings replace the former impractical limits.
    assert MAX_RESEARCH_SESSIONS == MAX_WEB_RESEARCH_SESSIONS == 100
    assert MAX_RESEARCH_CALLS == 20_000


def test_failed_attempts_remain_bounded_across_resumes():
    """Persist incomplete paid attempts so repeated resumes cannot exceed disclosure."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,))
    state = new_state(config, "e" * 32)
    persisted = []

    def incomplete_request(_key, _model, _messages, **options):
        """Consume all four local attempts, then report an incomplete generation."""
        for _attempt in range(MAX_REQUESTS_PER_UNIT):
            # The production adapter invokes this immediately before each provider call.
            options["on_attempt"]()
        # No external provider is contacted by this deterministic failure double.
        raise IncompleteGenerationError("partial", "length")

    maximum = config.workload()["maximum_provider_requests"]
    for _resume in range(MAX_REQUESTS_PER_UNIT):
        with pytest.raises(IncompleteGenerationError):
            list(
                run_research(
                    "secret",
                    config,
                    state,
                    lambda value: persisted.append(dict(value)),
                    request=incomplete_request,
                )
            )
    assert state["paid_request_attempts"] == maximum
    # A further resume stops before its fake can represent another provider request.
    with pytest.raises(RuntimeError, match="request ceiling"):
        list(run_research("secret", config, state, lambda _value: None, request=incomplete_request))
    assert state["paid_request_attempts"] == maximum
    assert persisted[-1]["paid_request_attempts"] == maximum


def test_legacy_research_reserves_unknown_paid_attempts():
    """Prevent a pre-accounting research snapshot from resetting paid spend to zero."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,))
    state = new_state(config, "f" * 32)
    state.pop("paid_request_attempts")
    # Parsing remains compatible for inspection but consumes every uncertain attempt.
    restored = parse_state(json.dumps(state), config, state["run_id"])
    assert restored["paid_request_attempts"] == config.workload()["maximum_provider_requests"]


@pytest.mark.parametrize("depths", [(-1, 1), (0, 1), (1, 2.5, 3), (True, 2)])
def test_research_config_validates_every_requested_depth(depths):
    """Reject invalid early and middle depths even when the final depth is valid."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, depths)
    # Validation must inspect each requested probe rather than relying on the maximum.
    # This prevents a run from silently omitting an invalid requested record.
    with pytest.raises(ValueError, match="Research depth is out of range"):
        config.validate()


def test_completed_records_loads_completed_research_schema(tmp_path):
    """Expose completed worker snapshots without applying the preview resume schema."""
    run_id = "c" * 32
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,))
    state = new_state(config, run_id)
    # This is the versioned record shape emitted by the asynchronous research worker.
    record = {
        "schema_version": state["schema_version"],
        "protocol_version": state["protocol_version"],
        "scoring_version": state["scoring_version"],
        "run_mode": "research",
        "run_id": run_id,
        "session": 1,
        "model": "openai/gpt-5.6-luna",
        "variant": "bare",
        "depth": 1,
        "score": 75.0,
    }
    state.update(status="complete", records=[record])
    # Operator-selected state paths need not encode the independently generated run ID.
    save_state(tmp_path / "operator-selected-state.json", state)
    # Preview-only fields and a filename-derived run ID are intentionally unnecessary.
    exported = completed_records(tmp_path)
    assert {key: value for key, value in exported[0].items() if not key.startswith("_")} == record
    assert exported[0]["_run_elapsed_seconds"] >= 0


def test_load_state_rejects_snapshot_from_repeating_prompt_protocol(tmp_path):
    """Reject paid partial results whose prompts came from the prior algorithm."""
    path = tmp_path / "legacy.json"
    run_id = "d" * 32
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=3)
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
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1, 2), seed=7)
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
    assert len(calls) - prior_calls == config.workload()["generation_units"] - 1
    assert any(message["content"] == "Reply 1." for message in calls[prior_calls])


def test_resume_rejects_changed_configuration_and_duplicate_units(tmp_path):
    """Reject changed semantics and duplicate idempotency units before paid work."""
    path = tmp_path / "run.json"
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=3)
    state = new_state(config, "b" * 32)
    state["units"] = [{"unit_id": "same"}, {"unit_id": "same"}]
    save_state(path, state)
    with pytest.raises(ValueError):
        load_state(path, config, "b" * 32)
    state["units"] = []
    save_state(path, state)
    changed = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=4)
    with pytest.raises(ValueError):
        load_state(path, changed, "b" * 32)


def _priced_request(cost):
    """Build a mocked provider call that reports ``cost`` for every attempt."""

    def request(_key, _model, _messages, *, on_attempt, on_cost, **_options):
        """Reserve the attempt, report its charge, and return a complete reply."""
        # Accounting mirrors the real adapter: reserve first, then report the charge.
        on_attempt()
        if cost is not None:
            on_cost(cost)
        return "Short reply."

    return request


def test_research_totals_reported_costs_for_every_paid_attempt():
    """Sum each priced attempt and keep the priced count equal to paid attempts."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=5)
    state = new_state(config, "e" * 32)

    list(run_research("secret", config, state, lambda _v: None, request=_priced_request(0.25)))

    # Every paid attempt was priced, so the total is complete.
    attempts = state["paid_request_attempts"]
    assert state["costed_request_attempts"] == attempts > 0
    assert state["provider_cost_usd"] == pytest.approx(0.25 * attempts)


def test_legacy_research_state_never_gains_a_partial_cost_total():
    """Leave pre-tracking snapshots without totals so earlier charges stay unknown."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=5)
    state = new_state(config, "f" * 32)
    # Model a snapshot written before cost tracking existed.
    del state["provider_cost_usd"], state["costed_request_attempts"]
    resumed = parse_state(json.dumps(state), config, "f" * 32)

    list(run_research("secret", config, resumed, lambda _v: None, request=_priced_request(0.25)))

    assert "provider_cost_usd" not in resumed
    assert "costed_request_attempts" not in resumed


def test_parse_state_drops_damaged_cost_totals():
    """Resume a snapshot with impossible cost data but stop claiming a total."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=5)
    state = new_state(config, "a" * 32)
    # More priced attempts than paid attempts cannot be a real history.
    state["costed_request_attempts"] = 3

    parsed = parse_state(json.dumps(state), config, "a" * 32)

    assert "provider_cost_usd" not in parsed and "costed_request_attempts" not in parsed


@pytest.mark.parametrize(("costed_delta", "exact"), [(0, True), (-1, False)])
def test_completed_records_export_exact_cost_only_when_every_attempt_was_priced(
    tmp_path, costed_delta, exact
):
    """Export the reported total when complete, and a flagged estimate otherwise."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=5)
    state = new_state(config, "b" * 32)
    list(run_research("secret", config, state, lambda _v: None, request=_priced_request(0.5)))
    # A shortfall models one attempt whose charge OpenRouter never reported.
    state["costed_request_attempts"] += costed_delta
    save_state(tmp_path / "run.json", state)

    records = completed_records(tmp_path)

    assert records
    if exact:
        assert all(r["_run_cost_usd"] == pytest.approx(state["provider_cost_usd"]) for r in records)
        assert all("_run_cost_estimated" not in r for r in records)
    else:
        # The incomplete reported total is replaced by an estimate from saved text.
        assert all(r["_run_cost_usd"] == pytest.approx(estimate_run_cost(state)) for r in records)
        assert all(r["_run_cost_estimated"] is True for r in records)


def test_estimate_rebuilds_conversation_input_and_prices_with_catalog():
    """Charge each turn for its instruction, earlier history, prompt, and reply."""
    model = "openai/gpt-5.6-luna"
    spec = MODELS_BY_ID[model]
    # Two turns of one conversation: the second re-sends the first exchange.
    units = [
        {
            "kind": "generation",
            "model": model,
            "session_id": "s",
            "variant": "bare",
            "depth": depth,
            "prompt": "p" * 40,
            "response": "r" * 80,
        }
        for depth in (2, 1)
    ]
    instruction = len(VARIANTS["bare"])
    input_chars = (instruction + 40) + (instruction + 120 + 40)
    expected = (
        input_chars / CHARS_PER_TOKEN * spec.input_usd_per_token
        + 160 / CHARS_PER_TOKEN * spec.output_usd_per_token
    )

    assert estimate_run_cost({"units": units}) == pytest.approx(expected)


@pytest.mark.parametrize(
    "units",
    [
        [],
        [
            {
                "kind": "generation",
                "model": "retired/model",
                "session_id": "s",
                "variant": "bare",
                "depth": 1,
                "prompt": "p",
                "response": "r",
            }
        ],
        [
            {
                "kind": "generation",
                "model": "openai/gpt-5.6-luna",
                "session_id": "s",
                "variant": "bare",
                "depth": 1,
                "prompt": "p",
            }
        ],
    ],
)
def test_estimate_is_unavailable_without_priced_complete_units(units):
    """Return no estimate for empty runs, uncatalogued models, or damaged units."""
    assert estimate_run_cost({"units": units}) is None


def test_reported_cost_is_persisted_before_the_reply_is_validated():
    """Save a reported charge even when the request then fails before its checkpoint."""
    config = ResearchConfig(("openai/gpt-5.6-luna",), 1, (1,), seed=5)
    state = new_state(config, "c" * 32)
    snapshots = []

    def request(_key, _model, _messages, *, on_attempt, on_cost, **_options):
        """Report a charge, then fail as a filtered or malformed reply would."""
        on_attempt()
        on_cost(0.4)
        raise IncompleteGenerationError("", "content_filter")

    with pytest.raises(IncompleteGenerationError):
        list(
            run_research(
                "secret",
                config,
                state,
                lambda value: snapshots.append(json.loads(json.dumps(value))),
                request=request,
            )
        )

    # The last durable snapshot already holds the charge and its priced attempt.
    assert snapshots[-1]["provider_cost_usd"] == pytest.approx(0.4)
    assert snapshots[-1]["costed_request_attempts"] == snapshots[-1]["paid_request_attempts"] == 1


def test_estimate_is_unavailable_for_malformed_depths():
    """Return no estimate, rather than crash the leaderboard, for unsortable depths."""
    units = [
        {
            "kind": "generation",
            "model": "openai/gpt-5.6-luna",
            "session_id": "s",
            "variant": "bare",
            "depth": depth,
            "prompt": "p",
            "response": "r",
        }
        for depth in (1, "2")
    ]
    assert estimate_run_cost({"units": units}) is None


def test_estimate_is_unavailable_for_unknown_unit_kinds():
    """Refuse an estimate rather than silently skipping a request it cannot price."""
    assert estimate_run_cost({"units": [{"kind": "mystery"}]}) is None
