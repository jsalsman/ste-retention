import json
import os
import threading

_RECORDS_LOCK = threading.Lock()


def get_records_file():
    """Returns the correct path for records depending on Cloud Run environment."""
    if os.path.exists("/experiments") and os.path.isdir("/experiments"):
        return "/experiments/records.jsonl"
    return "ste_retention_run/records.jsonl"


def append_record(record, records_file=None):
    """
    Thread-safely appends a JSON record to the records file.
    Uses a global threading.Lock to ensure thread safety without the GIL.
    """
    if records_file is None:
        records_file = get_records_file()

    with _RECORDS_LOCK:
        # Ensure the directory exists
        os.makedirs(
            os.path.dirname(records_file) if os.path.dirname(records_file) else ".", exist_ok=True
        )
        with open(records_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
