#!/usr/bin/env python3
"""Command-line entry point for a deliberately bounded STE retention run."""

import argparse
import os
from pathlib import Path
from uuid import uuid4

from experiment import ALLOWED_MODELS, run_experiment, validate_run
from records import append_records

# The CLI alone reads the environment, then passes the credential explicitly.
ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "ste_retention_run" / "records.jsonl"


def main() -> int:
    """Run a bounded CLI experiment after confirmation and persist safe results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", choices=sorted(ALLOWED_MODELS), default="google/gemini-2.0-flash-001"
    )
    parser.add_argument("--batches", type=int, default=1)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    args = parser.parse_args()
    try:
        model, batches, turns = validate_run(args.model, args.batches, args.turns)
    except ValueError as exc:
        parser.error(str(exc))
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        parser.error("Set OPENROUTER_API_KEY before running the experiment.")
    # Interactive confirmation prevents an accidental paid invocation.
    work = batches * turns * 4
    if input(f"Run {work} bounded paid requests? [y/N] ").strip().lower() != "y":
        return 0
    persisted = 0
    # Each invocation is analytically distinct, even when a failed command is retried.
    run_id = uuid4().hex

    def checkpoint(records: list[dict]) -> None:
        """Append only responses completed since the preceding durable checkpoint."""
        nonlocal persisted
        # The orchestrator supplies cumulative snapshots, so slicing avoids duplicate lines.
        new_records = records[persisted:]
        if new_records:
            append_records(args.records, new_records)
            # Advance only after append_records flushes every newly completed response.
            persisted = len(records)

    # Checkpoint callbacks run before progress is printed and survive a later provider failure.
    for event in run_experiment(api_key, model, batches, turns, persist=checkpoint, run_id=run_id):
        print(event["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
