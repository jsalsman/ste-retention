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

from ste.models.openrouter import chat
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
# limits. Operators can choose smaller values and must confirm the workload.
MAX_RESEARCH_SESSIONS = 10_000
MAX_RESEARCH_DEPTH = 128
MAX_RESEARCH_CALLS = 1_000_000


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
    max_tokens: int = 600

    def validate(self) -> None:
        """Reject unsafe, unsupported, or internally inconsistent configuration."""
        if not self.models or any(model not in ALLOWED_MODELS for model in self.models):
            raise ValueError("Every research model must be supported.")
        if type(self.sessions) is not int or not 1 <= self.sessions <= MAX_RESEARCH_SESSIONS:
            raise ValueError("Research session count is out of range.")
        if not self.depths or tuple(sorted(set(self.depths))) != self.depths:
            raise ValueError("Probe depths must be unique positive values in ascending order.")
        if type(self.depths[-1]) is not int or not 1 <= self.depths[-1] <= MAX_RESEARCH_DEPTH:
            raise ValueError("Research depth is out of range.")
        if self.judge_model is not None and self.judge_model not in ALLOWED_MODELS:
            raise ValueError("The judge model must be supported.")
        if self.budget_usd <= 0 or self.provider_timeout <= 0 or self.judge_timeout <= 0:
            raise ValueError("Budgets and timeouts must be positive.")
        if self.workload()["total_calls"] > MAX_RESEARCH_CALLS:
            raise ValueError("The research workload exceeds the safety limit.")

    def workload(self) -> dict[str, int]:
        """Return generation and separately identifiable judge call counts."""
        generation = len(self.models) * self.sessions * len(VARIANTS) * max(self.depths)
        judges = len(self.models) * self.sessions * len(VARIANTS) * len(self.depths)
        judges = judges if self.judge_model else 0
        # Keeping judge calls separate makes paid confirmation meaningful.
        return {
            "generation_calls": generation,
            "judge_calls": judges,
            "total_calls": generation + judges,
        }


def prompt_sequence(seed: int, session_id: str, turns: int) -> tuple[str, tuple[str, ...]]:
    """Create a deterministic sequence identity and prompts shared by all arms."""
    generator = random.Random(f"{seed}:{session_id}")  # noqa: S311 - experimental reproducibility
    prompts = tuple(generator.choice(PROMPT_POOL) for _ in range(turns))
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


def load_state(path: Path, config: ResearchConfig, run_id: str) -> dict:
    """Load an explicitly named run and validate all resume-critical semantics."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
    return state


def run_research(
    api_key: str,
    config: ResearchConfig,
    state: dict,
    persist: Callable[[dict], None],
    *,
    request: Callable[..., str] = chat,
    approved_words: set[str] | frozenset[str] | None = None,
) -> Iterator[dict]:
    """Run or resume every arm, persisting each inference before reporting it."""
    config.validate()
    completed = {unit["unit_id"]: unit for unit in state["units"]}
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
                                max_tokens=40,
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
        f"Workload: {workload['generation_calls']} generation + "
        f"{workload['judge_calls']} judge calls; "
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
