"""Resumable, transport-neutral orchestration for the complete research study."""

import argparse
import json
import os
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ste.models.openrouter import DEFAULT_MAX_TOKENS, MAX_CONTINUATIONS, chat_text
from ste.protocol import (
    ALLOWED_MODELS,
    DEFAULT_RESEARCH_DEPTHS,
    PROTOCOL_VERSION,
    PROMPT_POOL,
    SCHEMA_VERSION,
    SCORING_VERSION,
    VARIANTS,
)
from ste.scoring import load_approved_words, parse_judge_score, score_text

# Research limits protect unattended jobs without inheriting the web preview's
# limits. The paid-request ceiling includes every allowed continuation attempt,
# while logical-unit totals remain suitable for checkpoint progress reporting.
MAX_RESEARCH_SESSIONS = 10_000
MAX_RESEARCH_DEPTH = 128
MAX_RESEARCH_CALLS = 1_000_000
MAX_REQUESTS_PER_UNIT = MAX_CONTINUATIONS + 1
# The web study fixes one model, no judge, four variants, and the default deepest
# turn, so this derived bound is the largest session count below the paid ceiling.
MAX_WEB_RESEARCH_SESSIONS = MAX_RESEARCH_CALLS // (
    len(VARIANTS) * max(DEFAULT_RESEARCH_DEPTHS) * MAX_REQUESTS_PER_UNIT
)


@dataclass(frozen=True)
class ResearchConfig:
    """Describe one immutable research run and all resume-critical settings."""

    models: tuple[str, ...]
    sessions: int
    depths: tuple[int, ...] = DEFAULT_RESEARCH_DEPTHS
    seed: int = 0
    budget_usd: float = 40.0
    provider_timeout: float = 120.0
    judge_model: str | None = None
    judge_timeout: float = 120.0
    max_tokens: int = DEFAULT_MAX_TOKENS

    def validate(self) -> None:
        """Reject unsafe, unsupported, or internally inconsistent configuration."""
        if not self.models or any(model not in ALLOWED_MODELS for model in self.models):
            raise ValueError("Every research model must be supported.")
        if type(self.sessions) is not int or not 1 <= self.sessions <= MAX_RESEARCH_SESSIONS:
            raise ValueError("Research session count is out of range.")
        if not self.depths:
            raise ValueError("At least one probe depth is required.")
        # Validate every value before sorting so malformed mixed types fail predictably.
        # Exact integer checks also reject booleans, which otherwise behave like 0 and 1.
        if any(
            type(depth) is not int or not 1 <= depth <= MAX_RESEARCH_DEPTH for depth in self.depths
        ):
            raise ValueError("Research depth is out of range.")
        if tuple(sorted(set(self.depths))) != self.depths:
            raise ValueError("Probe depths must be unique values in ascending order.")
        # The current protocol presents each pool entry at most once per session.
        # A future protocol must be versioned before it may deliberately repeat prompts.
        if self.depths[-1] > len(PROMPT_POOL):
            raise ValueError("Research depth exceeds the non-repeating prompt pool.")
        if self.judge_model is not None and self.judge_model not in ALLOWED_MODELS:
            raise ValueError("The judge model must be supported.")
        if self.budget_usd <= 0 or self.provider_timeout <= 0 or self.judge_timeout <= 0:
            raise ValueError("Budgets and timeouts must be positive.")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            # A stable positive allowance is resume-critical and bounds every generation.
            raise ValueError("The generation token limit must be a positive integer.")
        if self.workload()["maximum_provider_requests"] > MAX_RESEARCH_CALLS:
            # Enforce the worst case, not only the number of durable logical units.
            raise ValueError("The research workload exceeds the paid-request safety limit.")

    def workload(self) -> dict[str, int]:
        """Return logical units and maximum paid provider-request counts.

        Generation and judge units each normally use one provider request. A
        token-limited response can use ``MAX_REQUESTS_PER_UNIT`` requests, so
        safety validation and operator confirmation use the maximum values while
        streaming progress uses logical units that correspond to checkpoints.
        """
        generation_units = len(self.models) * self.sessions * len(VARIANTS) * max(self.depths)
        judge_units = len(self.models) * self.sessions * len(VARIANTS) * len(self.depths)
        judge_units = judge_units if self.judge_model else 0
        # Both kinds use the same bounded complete-output adapter and retry allowance.
        return {
            "generation_units": generation_units,
            "judge_units": judge_units,
            "total_units": generation_units + judge_units,
            "maximum_generation_requests": generation_units * MAX_REQUESTS_PER_UNIT,
            "maximum_judge_requests": judge_units * MAX_REQUESTS_PER_UNIT,
            "maximum_provider_requests": (generation_units + judge_units) * MAX_REQUESTS_PER_UNIT,
        }


def prompt_sequence(seed: int, session_id: str, turns: int) -> tuple[str, tuple[str, ...]]:
    """Create a deterministic, non-repeating prompt sequence shared by all arms."""
    if type(turns) is not int or not 1 <= turns <= len(PROMPT_POOL):
        # Fail explicitly rather than allowing ``random.sample`` to expose internals.
        raise ValueError("Prompt turns must fit within the non-repeating prompt pool.")
    generator = random.Random(f"{seed}:{session_id}")  # noqa: S311 - experimental reproducibility
    # Sampling without replacement preserves the historical experimental protocol.
    prompts = tuple(generator.sample(PROMPT_POOL, turns))
    digest = sha256(
        (str(seed) + "\0" + session_id + "\0" + "\0".join(prompts)).encode()
    ).hexdigest()
    # The full digest prevents accidental collision across independent invocations.
    return digest, prompts


def new_state(config: ResearchConfig, run_id: str | None = None) -> dict:
    """Create credential-free research state with a stable unpredictable run ID."""
    config.validate()
    identifier = run_id or uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "scoring_version": SCORING_VERSION,
        "run_mode": "research",
        "run_id": identifier,
        "config": {**config.__dict__, "models": list(config.models), "depths": list(config.depths)},
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "units": [],
        "records": [],
        # Paid attempts are durable separately because incomplete units have no record.
        "paid_request_attempts": 0,
    }


def save_state(path: Path, state: dict) -> None:
    """Atomically replace a research snapshot after flushing completed paid work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    # Compact JSON reduces the window spent replacing a growing operational file.
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_state(contents: str | bytes, config: ResearchConfig, run_id: str) -> dict:
    """Decode and validate research content from either persistence backend."""
    try:
        # Both web API reads and CLI filesystem reads share every semantic check.
        state = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("The research snapshot is unavailable or malformed.") from exc
    expected = new_state(config, run_id)["config"]
    versions = (
        state.get("schema_version"),
        state.get("protocol_version"),
        state.get("scoring_version"),
    )
    if versions != (SCHEMA_VERSION, PROTOCOL_VERSION, SCORING_VERSION):
        raise ValueError("The research snapshot uses incompatible versions.")
    if (
        state.get("run_id") != run_id
        or state.get("run_mode") != "research"
        or state.get("config") != expected
    ):
        raise ValueError("The research snapshot does not match the requested run configuration.")
    units = state.get("units")
    if not isinstance(units, list) or len(
        {unit.get("unit_id") for unit in units if isinstance(unit, dict)}
    ) != len(units):
        raise ValueError("The research snapshot contains duplicate or malformed units.")
    paid_attempts = state.setdefault("paid_request_attempts", 0)
    maximum_attempts = config.workload()["maximum_provider_requests"]
    if type(paid_attempts) is not int or not 0 <= paid_attempts <= maximum_attempts:
        # Never resume state that can bypass or has already exceeded its paid ceiling.
        raise ValueError("The research snapshot has invalid paid-request accounting.")
    return state


def load_state(path: Path, config: ResearchConfig, run_id: str) -> dict:
    """Load an explicitly named file and validate all resume-critical semantics."""
    try:
        # The CLI stays file-based and intentionally takes no web-service lease.
        contents = path.read_bytes()
    except OSError as exc:
        raise ValueError("The research snapshot is unavailable or malformed.") from exc
    return parse_state(contents, config, run_id)


def run_research(
    api_key: str,
    config: ResearchConfig,
    state: dict,
    persist: Callable[[dict], None],
    *,
    request: Callable[..., str] = chat_text,
    approved_words: set[str] | frozenset[str] | None = None,
) -> Iterator[dict]:
    """Run or resume every arm, persisting each inference before reporting it."""
    config.validate()
    completed = {unit["unit_id"]: unit for unit in state["units"]}
    maximum_attempts = config.workload()["maximum_provider_requests"]

    def account_attempt() -> None:
        """Reserve and persist one paid request before contacting the provider.

        The durable counter spans retries and resumes, so repeated incomplete
        generations cannot exceed the originally disclosed worst-case request
        ceiling. Persisting first can conservatively overcount an interrupted call,
        but it cannot conceal a request that might have reached the provider.
        """
        paid_attempts = state.get("paid_request_attempts", 0)
        if type(paid_attempts) is not int or paid_attempts >= maximum_attempts:
            # Stop before another request can exceed the confirmed workload ceiling.
            raise RuntimeError("The paid-provider request ceiling was reached.")
        state["paid_request_attempts"] = paid_attempts + 1
        # This checkpoint contains no credential and precedes the external request.
        persist(state)

    for model in config.models:
        for session in range(1, config.sessions + 1):
            session_id = f"{model}:{session}"
            sequence_id, prompts = prompt_sequence(config.seed, session_id, max(config.depths))
            for variant, instruction in VARIANTS.items():
                history: list[dict[str, str]] = []
                for depth, prompt in enumerate(prompts, 1):
                    unit_id = sha256(
                        f"{state['run_id']}:{session_id}:{variant}:{depth}:generate".encode()
                    ).hexdigest()
                    if unit_id in completed:
                        reply = completed[unit_id].get("response")
                        if not isinstance(reply, str):
                            raise ValueError("A completed research unit has no usable response.")
                    else:
                        messages = [
                            {"role": "system", "content": instruction},
                            *history,
                            {"role": "user", "content": prompt},
                        ]
                        reply = request(
                            api_key,
                            model,
                            messages,
                            timeout=config.provider_timeout,
                            max_tokens=config.max_tokens,
                            on_attempt=account_attempt,
                        )
                        unit = {
                            "unit_id": unit_id,
                            "kind": "generation",
                            "model": model,
                            "session_id": session_id,
                            "variant": variant,
                            "depth": depth,
                            "prompt": prompt,
                            "response": reply,
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        state["units"].append(unit)
                        completed[unit_id] = unit
                        persist(state)
                        # Reporting follows the durable checkpoint, never precedes it.
                        yield {"type": "unit", "unit_id": unit_id, "kind": "generation"}
                    history.extend(
                        (
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": reply},
                        )
                    )
                    if depth not in config.depths:
                        continue
                    judge_value = None
                    if config.judge_model:
                        judge_id = sha256(
                            f"{state['run_id']}:{session_id}:{variant}:{depth}:judge".encode()
                        ).hexdigest()
                        if judge_id not in completed:
                            judge_prompt = (
                                'Return only JSON as {"score": number from 0 through 100}. '
                                "Score STE compliance.\n" + reply
                            )
                            raw = request(
                                api_key,
                                config.judge_model,
                                [{"role": "user", "content": judge_prompt}],
                                timeout=config.judge_timeout,
                                # JSON judging needs less output, but 256 tokens avoids
                                # truncating valid provider wrappers before their stop.
                                max_tokens=256,
                                on_attempt=account_attempt,
                            )
                            judge_value = parse_judge_score(raw)
                            judge_unit = {
                                "unit_id": judge_id,
                                "kind": "judge",
                                "score": judge_value,
                                "model": config.judge_model,
                                "completed_at": datetime.now(timezone.utc).isoformat(),
                            }
                            state["units"].append(judge_unit)
                            completed[judge_id] = judge_unit
                            persist(state)
                            yield {"type": "unit", "unit_id": judge_id, "kind": "judge"}
                        else:
                            judge_value = completed[judge_id]["score"]
                    record_id = f"{session_id}:{variant}:{depth}"
                    if any(record.get("record_id") == record_id for record in state["records"]):
                        continue
                    metrics = score_text(reply, approved_words, judge_value)
                    record = {
                        "record_id": record_id,
                        "schema_version": SCHEMA_VERSION,
                        "protocol_version": PROTOCOL_VERSION,
                        "scoring_version": SCORING_VERSION,
                        "run_mode": "research",
                        "run_id": state["run_id"],
                        "session": session,
                        "session_id": session_id,
                        "model": model,
                        "variant": variant,
                        "depth": depth,
                        "prompt_id": sha256(prompt.encode()).hexdigest()[:16],
                        "prompt_sequence_id": sequence_id,
                        "score": metrics["score"],
                        "metrics": metrics,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    state["records"].append(record)
                    persist(state)
    state["status"] = "complete"
    persist(state)
    yield {"type": "success", "run_id": state["run_id"], "records": len(state["records"])}


def main(argv: list[str] | None = None) -> int:
    """Confirm cost/workload, then execute an explicitly resumable research job."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--sessions", type=int, default=6)
    parser.add_argument("--depths", nargs="+", type=int, default=list(DEFAULT_RESEARCH_DEPTHS))
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--budget-usd", type=float, default=40.0)
    parser.add_argument("--approved-words", type=Path)
    parser.add_argument("--judge-model")
    parser.add_argument("--provider-timeout", type=float, default=120.0)
    parser.add_argument("--judge-timeout", type=float, default=120.0)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run-id", help="Explicit existing run ID to resume")
    parser.add_argument("--yes", action="store_true", help="Confirm the displayed paid workload")
    args = parser.parse_args(argv)
    config = ResearchConfig(
        tuple(args.models),
        args.sessions,
        tuple(args.depths),
        args.seed,
        args.budget_usd,
        args.provider_timeout,
        args.judge_model,
        args.judge_timeout,
    )
    config.validate()
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        parser.error("Set OPENROUTER_API_KEY before starting paid work.")
    state = load_state(args.state, config, args.run_id) if args.run_id else new_state(config)
    workload = config.workload()
    print(
        f"Workload: {workload['generation_units']} generation + "
        f"{workload['judge_units']} judge logical units; "
        f"at most {workload['maximum_provider_requests']} paid provider requests; "
        f"budget cap ${config.budget_usd:.2f}."
    )
    if not args.yes:
        parser.error("Review the workload and pass --yes to confirm paid work.")
    words = load_approved_words(args.approved_words)
    save_state(args.state, state)
    for event in run_research(
        api_key, config, state, lambda value: save_state(args.state, value), approved_words=words
    ):
        print(json.dumps(event, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
