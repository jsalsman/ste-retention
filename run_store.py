"""Durable, credential-free snapshots for resumable bounded experiments."""

import json
import math
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Run identifiers are deliberately narrow because they become file names below.
RUN_ID = re.compile(r"^[a-f0-9]{32}$")
SCHEMA_VERSION = 1
VALID_VARIANTS = frozenset({"bare", "rules", "named", "named_rules"})
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class RunStoreError(ValueError):
    """Describe invalid or unavailable persisted run state without exposing content."""


def _lock(run_id: str) -> threading.Lock:
    """Return the process-local lock that serializes updates for one run identifier."""
    with _LOCKS_GUARD:
        # A lock prevents native Gunicorn threads from corrupting one run snapshot.
        lock = _LOCKS.setdefault(run_id, threading.Lock())
    # Multi-instance exclusion belongs at the service/routing layer when using GCS FUSE.
    return lock


def _path(directory: Path, run_id: str) -> Path:
    """Resolve a validated run identifier beneath the configured persistence directory."""
    if not RUN_ID.fullmatch(run_id):
        # Reject separators and traversal before constructing a filesystem path.
        raise RunStoreError("The experiment run identifier is invalid.")
    return directory / f"{run_id}.json"


def create_run(directory: Path, model: str, batches: int, turns: int) -> dict:
    """Create and durably save metadata for a new bounded experiment run."""
    run_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    # Credentials are intentionally absent from the complete persisted schema.
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "model": model,
        "batches": batches,
        "turns": turns,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "records": [],
    }
    save_run(directory, state)
    return state


def load_run(directory: Path, run_id: str) -> dict:
    """Load and validate a saved run snapshot for safe resumption."""
    path = _path(directory, run_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunStoreError("The requested experiment run was not found.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunStoreError("The saved experiment run is unavailable or malformed.") from exc
    required = {"schema_version", "run_id", "model", "batches", "turns", "status", "records"}
    # Reject unknown historical formats rather than guessing how to resume paid work.
    if not isinstance(state, dict) or not required.issubset(state) or state["schema_version"] != 1:
        raise RunStoreError("The saved experiment run uses an unsupported schema.")
    if state["run_id"] != run_id or not isinstance(state["records"], list):
        raise RunStoreError("The saved experiment run is malformed.")
    if state["status"] not in {"pending", "running", "interrupted", "complete"}:
        raise RunStoreError("The saved experiment run has an invalid status.")
    if type(state["batches"]) is not int or type(state["turns"]) is not int:
        raise RunStoreError("The saved experiment run has invalid settings.")
    seen = set()
    for record in state["records"]:
        # Validate the fields needed to reconstruct context before opening a stream.
        required_record = {"session", "model", "variant", "depth", "score", "text"}
        if not isinstance(record, dict) or not required_record.issubset(record):
            raise RunStoreError("The saved experiment run contains a malformed record.")
        key = (record["session"], record["variant"], record["depth"])
        score = record["score"]
        valid_position = (
            type(record["session"]) is int
            and 1 <= record["session"] <= state["batches"]
            and record["variant"] in VALID_VARIANTS
            and type(record["depth"]) is int
            and 1 <= record["depth"] <= state["turns"]
        )
        valid_score = (
            not isinstance(score, bool)
            and isinstance(score, (int, float))
            and math.isfinite(score)
            and 0 <= score <= 100
        )
        if (
            key in seen
            or not valid_position
            or not valid_score
            or record["model"] != state["model"]
            or not isinstance(record["text"], str)
        ):
            raise RunStoreError("The saved experiment run contains inconsistent records.")
        # Credentials cannot appear as structured persistence fields at any depth.
        if any(name.lower() in {"api_key", "authorization"} for name in record):
            raise RunStoreError("The saved experiment run contains a forbidden field.")
        seen.add(key)
    return state


def save_run(directory: Path, state: dict) -> None:
    """Replace one run snapshot after flushing it, suitable for a mounted durable volume."""
    run_id = str(state.get("run_id", ""))
    path = _path(directory, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Defensive serialization rejects accidental credential-shaped top-level fields.
    if any(key.lower() in {"api_key", "authorization"} for key in state):
        raise RunStoreError("Credential fields cannot be persisted.")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    snapshot = dict(state)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    with _lock(run_id), temporary.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        # Replacement prevents readers from observing a partly written JSON document.
        os.replace(temporary, path)


def completed_records(directory: Path) -> list[dict]:
    """Collect records only from successfully completed snapshots for leaderboard use."""
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            state = load_run(directory, path.stem)
        except RunStoreError:
            # One damaged run must not make all valid leaderboard data unavailable.
            continue
        if state["status"] == "complete":
            records.extend(state["records"])
    return records
