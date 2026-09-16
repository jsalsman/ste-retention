#!/usr/bin/env python3
"""
ste_retention.py

Primary question: does naming a writing standard (ASD-STE100) make a model hold
the constraint longer than describing the same rules without the name?
"""

import argparse
import os
import random
import sys

from ste.orchestration import (
    TASK_PROMPTS,
    VARIANTS,
    estimate_batch_cost,
    get_incomplete_session,
    run_session_generator,
)
from ste.orchestration.io import append_record, get_records_file
from ste.scoring import load_approved_words

MODELS = [
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-4o",
    "google/gemini-2.0-flash-001",
]

PRICING = {
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "openai/gpt-4o": (2.50, 10.00),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
    "mistralai/mistral-large": (2.00, 6.00),
}

DEPTH_SCHEDULE = [1, 6, 12]
DEPTH_EXTENSIONS = [[1, 10, 20], [1, 16, 32]]

SESSIONS_PER_BATCH = 6
MAX_BATCHES = 6
BUDGET_USD = 40.0

NOMINAL_ALPHA = 0.05
SEQUENTIAL_ALPHA = 0.0158

RECORDS_FILE = get_records_file()
OUTPUT_DIR = os.path.dirname(RECORDS_FILE)

JUDGE_MODEL = "anthropic/claude-sonnet-4.5"
APPROVED_WORDS_FILE = None


def main():
    parser = argparse.ArgumentParser(description="Run STE retention experiment")
    parser.add_argument("--api-key", help="OpenRouter API Key (overrides env var)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key or "PASTE-KEY-HERE" in api_key:
        print("Set OPENROUTER_API_KEY environment variable or pass --api-key.")
        sys.exit(1)

    if OUTPUT_DIR:
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    approved = load_approved_words(APPROVED_WORDS_FILE)
    if approved is None:
        print("No approved word list; vocabulary term omitted from the score.\n")

    depths = list(DEPTH_SCHEDULE)
    spend = [0.0]
    obs = {}

    est = estimate_batch_cost(MODELS, depths, len(depths), SESSIONS_PER_BATCH, JUDGE_MODEL, PRICING)
    print(f"Depths {depths}. {len(VARIANTS)} arms: {', '.join(VARIANTS)}.")
    print(f"Estimated {est:.2f} USD per batch, {SESSIONS_PER_BATCH} sessions per model.")
    print(f"Budget {BUDGET_USD:.2f} USD, up to {MAX_BATCHES} batches.")

    # Require confirmation when run interactively
    if sys.stdout.isatty():
        if input("Proceed? [y/N] ").strip().lower() != "y":
            return
    else:
        print("Running in non-interactive mode. Proceeding.")

    for batch in range(1, MAX_BATCHES + 1):
        print(f"\n--- batch {batch} (spent {spend[0]:.2f}) ---")
        for model in MODELS:
            for _ in range(SESSIONS_PER_BATCH):
                session_id, variants_to_run = get_incomplete_session(
                    RECORDS_FILE, model, max_depth=max(depths)
                )
                if not variants_to_run:
                    variants_to_run = list(VARIANTS.keys())

                rng = random.Random(session_id)
                prompts = rng.sample(TASK_PROMPTS, max(depths))

                for variant in variants_to_run:
                    try:
                        for event in run_session_generator(
                            model,
                            variant,
                            prompts,
                            depths,
                            approved,
                            session_id,
                            api_key,
                            judge_model=JUDGE_MODEL,
                        ):
                            if event["type"] in ("session_start", "turn_complete"):
                                from datetime import datetime, timezone

                                heartbeat = {
                                    "type": "heartbeat_" + event["type"],
                                    "session": session_id,
                                    "model": model,
                                    "variant": variant,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }
                                append_record(heartbeat, RECORDS_FILE)
                            elif event["type"] == "record":
                                r = event["record"]
                                d = r["depth"]
                                score = r["score"]
                                obs.setdefault((session_id, model, d), {})[variant] = score
                                append_record(r, RECORDS_FILE)
                    except Exception as exc:
                        print(f"  {model} s{session_id} {variant}: {exc}")
                        continue

            done = sum(1 for c in obs.values() if len(c) == len(VARIANTS))
            print(f"  {model}: {done} complete cells so far")

        print(f"\n  spent {spend[0]:.2f} USD")

        if spend[0] > BUDGET_USD:
            print("\nStop: budget reached.")
            break

    complete = sum(1 for c in obs.values() if len(c) == len(VARIANTS))
    print(f"\nTotal spend {spend[0]:.2f} USD, {complete} complete cells.")
    print(f"Records in {RECORDS_FILE}. Run make_leaderboard.py next.")


if __name__ == "__main__":
    main()
