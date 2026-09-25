"""Bounded experiment orchestration independent of Flask transport details."""

import random
import time
from collections.abc import Callable, Iterator

from datetime import datetime, timezone
from hashlib import sha256

from ste.models.openrouter import DEFAULT_MAX_TOKENS, MAX_CONTINUATIONS, chat_text
from ste.protocol import (
    ALLOWED_MODELS,
    PREVIEW_DEADLINE_SECONDS,
    PREVIEW_MAX_BATCHES,
    PREVIEW_MAX_TURNS,
    PREVIEW_MAX_WORK_UNITS,
    PREVIEW_PROVIDER_TIMEOUT_SECONDS,
    PROMPT_POOL,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
    VARIANTS,
)
from ste.scoring import score_text

# Compatibility aliases expose the single-sourced preview policy.
MAX_BATCHES = PREVIEW_MAX_BATCHES
MAX_TURNS = PREVIEW_MAX_TURNS
MAX_WORK_UNITS = PREVIEW_MAX_WORK_UNITS
# The preview's paid ceiling includes the initial request and every allowed
# continuation for each logical unit, even though typical runs use fewer calls.
MAX_PROVIDER_REQUESTS = MAX_WORK_UNITS * (MAX_CONTINUATIONS + 1)
INTERACTIVE_DEADLINE_SECONDS = PREVIEW_DEADLINE_SECONDS
UPSTREAM_TIMEOUT_SECONDS = PREVIEW_PROVIDER_TIMEOUT_SECONDS
PROMPTS = PROMPT_POOL


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
    logical_units = batches * turns * len(VARIANTS)
    if logical_units > MAX_WORK_UNITS:
        raise ValueError("The requested workload exceeds the interactive limit.")
    maximum_requests = logical_units * (MAX_CONTINUATIONS + 1)
    if maximum_requests > MAX_PROVIDER_REQUESTS:
        # Apply the paid-work ceiling to worst-case continuations, not typical calls.
        raise ValueError("The requested paid-provider workload exceeds the interactive limit.")
    return model, batches, turns


def run_experiment(
    api_key: str,
    model: str,
    batches: int,
    turns: int,
    *,
    request: Callable[..., str] = chat_text,
    clock: Callable[[], float] = time.monotonic,
    existing_records: list[dict] | None = None,
    persist: Callable[[list[dict]], None] | None = None,
    on_attempt: Callable[[], None] | None = None,
    run_id: str | None = None,
    seed: int = 1,
) -> Iterator[dict]:
    """Yield progress while skipping saved units and checkpointing each new response.

    ``on_attempt`` is forwarded to the provider adapter so the web layer can
    durably reserve paid requests independently from completed record checkpoints.
    Injected test transports may ignore it because they make no paid calls.
    """
    model, batches, turns = validate_run(model, batches, turns)
    total = batches * turns * len(VARIANTS)
    started = clock()
    durations: list[float] = []
    # Copy checkpoints so normalization never mutates state owned by the caller.
    records: list[dict] = [dict(record) for record in existing_records or []]
    if run_id is not None:
        for record in records:
            # Snapshots written before durable IDs belong to the run being resumed.
            if record.get("run_id") is None:
                record["run_id"] = run_id
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
        prompts = random.Random(f"{seed}:{batch}").sample(PROMPTS, turns)  # noqa: S311 - reproducible design
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
                elapsed = max(0.0, clock() - started)
                remaining = INTERACTIVE_DEADLINE_SECONDS - elapsed
                if remaining <= 0:
                    # Never start more paid work after the synchronous request budget expires.
                    raise TimeoutError("The interactive experiment deadline was reached.")
                unit_started = clock()
                messages = [
                    {"role": "system", "content": instruction},
                    *history,
                    {"role": "user", "content": prompt},
                ]
                # Leave room for scoring, checkpoint I/O, streaming, and deployment overhead.
                timeout = min(UPSTREAM_TIMEOUT_SECONDS, remaining)
                reply = request(
                    api_key,
                    model,
                    messages,
                    timeout=timeout,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    on_attempt=on_attempt,
                )
                # Context is retained within an arm, matching the retention design.
                history.extend(
                    ({"role": "user", "content": prompt}, {"role": "assistant", "content": reply})
                )
                metrics = score_text(reply)
                prompt_id = sha256(prompt.encode()).hexdigest()[:16]
                sequence_id = sha256(f"{seed}:{batch}".encode()).hexdigest()[:16]
                records.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "protocol_version": PROTOCOL_VERSION,
                        "scoring_version": SCORING_VERSION,
                        "run_mode": "preview",
                        # A durable identifier keeps retries from contaminating paired analyses.
                        "run_id": run_id,
                        "session": batch,
                        "session_id": str(batch),
                        "model": model,
                        "variant": variant,
                        "depth": turn,
                        "prompt_id": prompt_id,
                        "prompt_sequence_id": sequence_id,
                        "score": metrics["score"],
                        "metrics": metrics,
                        "text": reply,
                        "created_at": datetime.now(timezone.utc).isoformat(),
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
