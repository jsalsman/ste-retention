#!/usr/bin/env python3
"""Thin CLI that safely renders JSONL experiment records to leaderboard HTML."""

import argparse
from pathlib import Path

from leaderboard import render_file, render_leaderboard
from records import RecordError

# Defaults are repository-relative so the CLI behaves predictably from any directory.
ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "ste_retention_run" / "records.jsonl"
DEFAULT_OUTPUT = ROOT / "leaderboard.html"


def main() -> int:
    """Parse file options, render safe HTML, and report actionable record errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        # Missing data is rendered as a useful empty state for deployments.
        output = render_file(args.records) if args.records.exists() else render_leaderboard([])
        args.output.write_text(output, encoding="utf-8")
    except (OSError, RecordError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
