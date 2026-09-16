#!/usr/bin/env python3
"""
make_leaderboard.py

CLI entry point for generating the leaderboard HTML.
"""

import os
import sys

from ste.leaderboard import generate_leaderboard_html
from ste.orchestration.io import get_records_file

RECORDS_FILE = get_records_file()
OUTPUT_HTML = "leaderboard.html"


def main():
    if not os.path.exists(RECORDS_FILE):
        print(f"No records at {RECORDS_FILE}.")
        sys.exit(1)

    html = generate_leaderboard_html(RECORDS_FILE, partial=False)
    if html.startswith("<p>"):
        print(html)
        sys.exit(1)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {OUTPUT_HTML}.")


if __name__ == "__main__":
    main()
