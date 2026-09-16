"""Bounded experiment orchestration independent of Flask transport details."""

import random
import time
from collections.abc import Callable, Iterator
from types import MappingProxyType

from openrouter import chat
from scoring import score_text

# Public interactive choices and strict caps prevent accidental long paid runs.
ALLOWED_MODELS = frozenset(
    {
        "anthropic/claude-sonnet-4.5",
        "openai/gpt-4o",
        "google/gemini-2.0-flash-001",
        "meta-llama/llama-3.3-70b-instruct",
    }
)
VARIANTS = MappingProxyType(
    {
        "bare": "Write all replies in simplified technical English.",
        "rules": (
            "Write in simplified technical English. Use active voice and "
            "sentences of at most 25 words."
        ),
        "named": "Write all replies in ASD-STE100 Simplified Technical English.",
        "named_rules": "Write in ASD-STE100. Use active voice and sentences of at most 25 words.",
    }
)
PROMPTS = (
    "Explain how a centrifugal pump moves fluid.",
    "Describe how to inspect a drive belt for wear.",
    "Explain how a pressure relief valve works.",
)
MAX_BATCHES = 2
MAX_TURNS = 3
MAX_WORK_UNITS = MAX_BATCHES * MAX_TURNS * len(VARIANTS)


def validate_run(model: object, batches: object, turns: object) -> tuple[str, int, int]:
    """Validate and normalize every paid-work parameter before streaming starts."""
    if not isinstance(model, str) or model not in ALLOWED_MODELS:
        # Do not permit arbitrary upstream model identifiers.
        raise ValueError("Select a supported model.")
    if type(batches) is not int or not 1 <= batches <= MAX_BATCHES:
        raise ValueError(f"Batches must be between 1 and {MAX_BATCHES}.")
    if type(turns) is not int or not 1 <= turns <= MAX_TURNS:
        raise ValueError(f"Turns must be between 1 and {MAX_TURNS}.")
    # Keep the explicit workload invariant visible at the boundary.
    if batches * turns * len(VARIANTS) > MAX_WORK_UNITS:
        raise ValueError("The requested workload exceeds the interactive limit.")
    return model, batches, turns


def run_experiment(
    api_key: str,
    model: str,
    batches: int,
    turns: int,
    *,
    request: Callable[..., str] = chat,
    clock: Callable[[], float] = time.monotonic,
    existing_records: list[dict] | None = None,
    persist: Callable[[list[dict]], None] | None = None,
    run_id: str | None = None,
) -> Iterator[dict]:
    """Yield progress while skipping saved units and checkpointing each new response."""
    model, batches, turns = validate_run(model, batches, turns)
    total = batches * turns * len(VARIANTS)
    started = clock()
    durations: list[float] = []
    records: list[dict] = list(existing_records or [])
    completed_keys = {
        (record.get("session"), record.get("variant"), record.get("depth")) for record in records
    }
    if len(completed_keys) != len(records) or len(records) > total:
        # Duplicate or excess records make a paid resume ambiguous, so fail closed.
        raise ValueError("Saved experiment progress is inconsistent.")
    yield {
        "type": "status",
        "message": "Experiment started.",
        "completed": len(records),
        "total": total,
        "elapsed_seconds": 0.0,
        "run_id": run_id,
    }
    for batch in range(1, batches + 1):
        prompts = random.Random(batch).sample(PROMPTS, turns)  # noqa: S311 - reproducible design
        for variant, instruction in VARIANTS.items():
            history: list[dict[str, str]] = []
            for turn, prompt in enumerate(prompts, 1):
                unit_key = (batch, variant, turn)
                if unit_key in completed_keys:
                    # Rebuild context from the saved answer without repeating paid inference.
                    saved = next(
                        record
                        for record in records
                        if (record.get("session"), record.get("variant"), record.get("depth"))
                        == unit_key
                    )
                    history.extend(
                        (
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": saved["text"]},
                        )
                    )
                    continue
                unit_started = clock()
                messages = [
                    {"role": "system", "content": instruction},
                    *history,
                    {"role": "user", "content": prompt},
                ]
                reply = request(api_key, model, messages, timeout=45, max_tokens=300)
                # Context is retained within an arm, matching the retention design.
                history.extend(
                    ({"role": "user", "content": prompt}, {"role": "assistant", "content": reply})
                )
                metrics = score_text(reply)
                records.append(
                    {
                        "session": batch,
                        "model": model,
                        "variant": variant,
                        "depth": turn,
                        "score": metrics["score"],
                        "metrics": metrics,
                        "text": reply,
                    }
                )
                completed_keys.add(unit_key)
                if persist is not None:
                    # Checkpoint before reporting completion so every advertised unit is durable.
                    persist(records)
                durations.append(max(0.0, clock() - unit_started))
                completed = len(records)
                event = {
                    "type": "status",
                    "message": f"Completed {variant}, turn {turn}.",
                    "completed": completed,
                    "total": total,
                    "elapsed_seconds": round(max(0.0, clock() - started), 2),
                    "run_id": run_id,
                }
                # Wait for two measurements, then smooth using all observed durations.
                if len(durations) >= 2:
                    event["eta_seconds"] = round(
                        sum(durations) / len(durations) * (total - completed), 1
                    )
                yield event
    yield {
        "type": "success",
        "message": "Experiment complete.",
        "completed": total,
        "total": total,
        "elapsed_seconds": round(max(0.0, clock() - started), 2),
        "records": records,
        "run_id": run_id,
    }
