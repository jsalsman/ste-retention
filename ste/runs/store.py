"""Durable, credential-free snapshots for resumable bounded experiments."""

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ste.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, SCORING_VERSION, VARIANTS
from ste.runs.backend import (
    LeaseUnavailableError,
    StorageBackend,
    get_backend,
    is_precondition_failed,
    read_blob,
)
from ste.runs.lease import RunActiveError

# Run identifiers are deliberately narrow because they become file names below.
RUN_ID = re.compile(r"^[a-f0-9]{32}$")
VALID_VARIANTS = frozenset(VARIANTS)


class RunStoreError(ValueError):
    """Describe invalid or unavailable persisted run state without exposing content."""


# Leaderboard discovery treats unreadable, invalid JSON, and schema-invalid snapshots
# uniformly so one damaged file cannot prevent publication of other completed runs.
SNAPSHOT_LOAD_ERRORS = (OSError, json.JSONDecodeError, RunStoreError)


def _path(directory: Path, run_id: str) -> Path:
    """Resolve a validated run identifier beneath the configured persistence directory."""
    if not RUN_ID.fullmatch(run_id):
        # Reject separators and traversal before constructing a filesystem path.
        raise RunStoreError("The experiment run identifier is invalid.")
    return directory / f"{run_id}.json"


def create_run(
    model: str,
    batches: int,
    turns: int,
    *,
    seed: int | None = None,
) -> dict:
    """Create credential-free metadata for a new bounded web experiment run."""
    run_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    # Credentials are intentionally absent from the complete persisted schema.
    state = {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "scoring_version": SCORING_VERSION,
        "run_mode": "preview",
        "seed": seed if seed is not None else int.from_bytes(os.urandom(8), "big"),
        "run_id": run_id,
        "model": model,
        "batches": batches,
        "turns": turns,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "records": [],
    }
    # The caller acquires a lease before conditionally creating this random run ID.
    return state


def parse_run(contents: str | bytes, run_id: str) -> dict:
    """Decode and validate preview snapshot content from either persistence backend."""
    try:
        # Central parsing keeps API and filesystem reads under identical validation.
        state = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RunStoreError("The saved experiment run is unavailable or malformed.") from exc
    required = {
        "schema_version",
        "protocol_version",
        "scoring_version",
        "run_mode",
        "seed",
        "run_id",
        "model",
        "batches",
        "turns",
        "status",
        "records",
    }
    # Reject unknown historical formats rather than guessing how to resume paid work.
    if (
        not isinstance(state, dict)
        or not required.issubset(state)
        or state["schema_version"] != SCHEMA_VERSION
    ):
        raise RunStoreError("The saved experiment run uses an unsupported schema.")
    if (
        state["run_id"] != run_id
        or state["protocol_version"] != PROTOCOL_VERSION
        or state["scoring_version"] != SCORING_VERSION
        or state["run_mode"] != "preview"
        or type(state["seed"]) is not int
        or not isinstance(state["records"], list)
    ):
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


def load_run(directory: Path, run_id: str) -> dict:
    """Load and validate a saved run snapshot for safe resumption."""
    path = _path(directory, run_id)
    try:
        # Ordinary status and leaderboard reads intentionally remain mount-based.
        contents = path.read_bytes()
    except FileNotFoundError as exc:
        raise RunStoreError("The requested experiment run was not found.") from exc
    except OSError as exc:
        raise RunStoreError("The saved experiment run is unavailable or malformed.") from exc
    return parse_run(contents, run_id)


class SnapshotFencer:
    """Read and conditionally write one leased web snapshot through Cloud Storage.

    The current GCS generation remains process memory only. Every Cloud Storage write
    is conditioned on that generation, preventing a stale lease owner from replacing
    a successor's checkpoint or recreating a conditionally deleted snapshot.
    """

    def __init__(self, directory: Path, run_id: str, backend: StorageBackend | None = None):
        """Bind a validated run path with no known generation for a new snapshot."""
        self.directory = directory
        self.run_id = run_id
        # Validate the identifier before deriving its Cloud Storage object name.
        _path(directory, run_id)
        # Reusing lease backend state avoids any second probe or client construction.
        self.backend = backend or get_backend(directory)
        self.generation = 0
        self.deleted = False
        self.fenced = False

    def read_bytes(self) -> bytes:
        """Read fresh snapshot bytes and remember the observed object generation."""
        # Bypass the mount cache so resumed paid work always sees the latest object.
        contents, generation = read_blob(
            self.backend.bucket, self.backend.object_name(f"{self.run_id}.json")
        )
        self.generation = generation
        return contents

    def load_run(self) -> dict:
        """Read a fresh preview snapshot and apply the shared preview validation."""
        # Parsing remains centralized in ``parse_run`` for both backend types.
        return parse_run(self.read_bytes(), self.run_id)

    def write(self, state: dict) -> None:
        """Persist a credential-free snapshot with a GCS generation precondition."""
        if self.deleted or self.fenced:
            raise RunActiveError("Experiment ownership changed; this request stopped.")
        # Defensive guards apply to terminal and cleanup writes as well as checkpoints.
        if any(key.lower() in {"api_key", "authorization"} for key in state):
            raise RunStoreError("Credential fields cannot be persisted.")
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        snapshot = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        # Web writes never use the mounted path because rename is not an atomic fence.
        blob = self.backend.bucket.blob(self.backend.object_name(f"{self.run_id}.json"))
        try:
            blob.upload_from_string(
                snapshot.encode("utf-8"),
                content_type="application/json",
                if_generation_match=self.generation,
            )
            self.generation = int(blob.generation)
        except Exception as exc:
            if not is_precondition_failed(exc):
                raise LeaseUnavailableError("Cloud Storage checkpoint write failed.") from exc
            self._adopt_ambiguous_write(state["updated_at"])

    def _adopt_ambiguous_write(self, updated_at: str) -> None:
        """Adopt a completed ambiguous upload or reject another owner's snapshot."""
        try:
            contents, generation = read_blob(
                self.backend.bucket, self.backend.object_name(f"{self.run_id}.json")
            )
            stored = json.loads(contents)
        except Exception as exc:
            raise RunActiveError("Experiment ownership changed; this request stopped.") from exc
        # updated_at is freshly generated for each attempted write and acts as retry ID.
        if not isinstance(stored, dict) or stored.get("updated_at") != updated_at:
            # Cleanup handlers see this flag and make no second API write attempt.
            self.fenced = True
            raise RunActiveError("Experiment ownership changed; this request stopped.")
        self.generation = generation

    def delete(self) -> None:
        """Delete the observed snapshot generation so stale writers remain fenced."""
        if self.generation in (None, 0):
            # Observe the exact generation before conditionally deleting it.
            self.read_bytes()
        blob = self.backend.bucket.blob(self.backend.object_name(f"{self.run_id}.json"))
        blob.delete(if_generation_match=self.generation)
        self.deleted = True


def _load_research_run(path: Path) -> dict:
    """Load one research snapshot and validate its leaderboard-facing contract.

    The snapshot's embedded ``run_id`` is authoritative: operators may choose any
    durable state filename, so the filename is deliberately not treated as identity.
    The returned dictionary is safe for :func:`completed_records` to inspect, but
    callers must still decide whether its status makes its records publishable.

    Args:
        path: Filesystem path of the candidate JSON research snapshot.

    Returns:
        The decoded research state when its versions, metadata, and every record
        satisfy the current protocol's leaderboard contract.

    Raises:
        RunStoreError: If the file cannot be decoded, has incompatible versions,
            lacks a usable run identifier, or contains an inconsistent record.

    """
    try:
        # Decode exactly once here rather than trusting the dispatch probe performed by
        # ``completed_records``; the file may have changed between the two reads.
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Research files are read-only here, so malformed snapshots are never resumed.
        raise RunStoreError("The research run is unavailable or malformed.") from exc
    # Read version markers defensively because arbitrary JSON files can share the
    # persistence directory and non-mapping values do not provide ``get``.
    versions = (
        state.get("schema_version") if isinstance(state, dict) else None,
        state.get("protocol_version") if isinstance(state, dict) else None,
        state.get("scoring_version") if isinstance(state, dict) else None,
    )
    # Keep research validation separate from the preview-only resume schema.
    if versions != (SCHEMA_VERSION, PROTOCOL_VERSION, SCORING_VERSION):
        raise RunStoreError("The research run uses an unsupported schema.")
    # The persisted identifier, rather than ``path.stem``, binds records to this run.
    # This permits documented operator-selected paths such as ``research/RUN.json``.
    run_id = state.get("run_id")
    if (
        not isinstance(run_id, str)
        or not RUN_ID.fullmatch(run_id)
        or state.get("run_mode") != "research"
        or state.get("status") not in {"pending", "running", "interrupted", "complete"}
        or not isinstance(state.get("records"), list)
    ):
        raise RunStoreError("The research run is malformed.")
    # Validate every exported observation independently; a single malformed record
    # excludes the snapshot instead of leaking partial or misleading results.
    for record in state["records"]:
        # The leaderboard consumes these identity, grouping, and numeric fields.
        required = {"schema_version", "protocol_version", "scoring_version", "run_mode"}
        required.update({"run_id", "session", "model", "variant", "depth", "score"})
        # Booleans are numeric subclasses, so score validation rejects them explicitly.
        score = record.get("score") if isinstance(record, dict) else None
        valid_score = (
            not isinstance(score, bool)
            and isinstance(score, (int, float))
            and math.isfinite(score)
            and 0 <= score <= 100
        )
        # Each record repeats the durable contract so exported rows remain independently
        # auditable after they are separated from their source snapshot.
        if (
            not isinstance(record, dict)
            or not required.issubset(record)
            or record["schema_version"] != SCHEMA_VERSION
            or record["protocol_version"] != PROTOCOL_VERSION
            or record["scoring_version"] != SCORING_VERSION
            or record["run_mode"] != "research"
            or record["run_id"] != run_id
            or record["variant"] not in VALID_VARIANTS
            or type(record["session"]) is not int
            or type(record["depth"]) is not int
            or not isinstance(record["model"], str)
            or not valid_score
        ):
            raise RunStoreError("The research run contains an inconsistent record.")
    # Return only after all records agree with their parent run and current versions.
    return state


def completed_records(directory: Path) -> list[dict]:
    """Collect records only from successfully completed snapshots for leaderboard use."""
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            # Dispatch on the minimal mode marker before applying a mode-specific schema.
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("run_mode") == "research":
                state = _load_research_run(path)
            else:
                state = load_run(directory, path.stem)
        except SNAPSHOT_LOAD_ERRORS:
            # One damaged run must not make all valid leaderboard data unavailable.
            continue
        if state["status"] == "complete" and state.get("run_mode") == "research":
            records.extend(state["records"])
    return records
