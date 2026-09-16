import json
import os
import threading

_RECORDS_LOCK = threading.Lock()


def get_experiments_dir():
    """Returns the correct path for experiments depending on Cloud Run environment."""
    if os.path.exists("/experiments") and os.path.isdir("/experiments"):
        return "/experiments"
    return "ste_retention_run"


def append_record(record, records_file):
    """
    Thread-safely appends a JSON record to the records file.
    Uses a global threading.Lock to ensure thread safety without the GIL.
    """
    with _RECORDS_LOCK:
        os.makedirs(os.path.dirname(records_file), exist_ok=True)
        with open(records_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def acquire_resume_lock(session_id, completed_count, base_dir=None):
    """
    Attempts to atomically acquire a lock file for a specific session resumption state.
    Utilizes O_CREAT | O_EXCL, which GCS FUSE translates to strongly consistent
    Google Cloud Storage preconditions (If-Generation-Match: 0).
    """
    if base_dir is None:
        base_dir = get_experiments_dir()

    lock_dir = os.path.join(base_dir, "locks")
    os.makedirs(lock_dir, exist_ok=True)

    lock_path = os.path.join(lock_dir, f"resume_{session_id}_{completed_count}.lock")

    try:
        # Atomic file creation. If the worker succeeds but crashes before finishing,
        # the lock persists. This acts as an implicit retry-limit, preventing infinite
        # crash loops on a bad payload state.
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
