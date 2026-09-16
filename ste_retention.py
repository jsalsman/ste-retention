#!/usr/bin/env python3
"""Command-line entry point for a deliberately bounded STE retention run."""

import argparse
import os
from pathlib import Path

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
    for event in run_experiment(api_key, model, batches, turns):
        print(event["message"])
        # Only the terminal event contains safe experiment records.
        if event["type"] == "success":
            append_records(args.records, event["records"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
