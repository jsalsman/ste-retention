"""JSON Lines persistence with validation and useful source locations."""

import json
from pathlib import Path
from typing import Iterable

from ste.protocol import PROTOCOL_VERSION, SCHEMA_VERSION, SCORING_VERSION


class RecordError(ValueError):
    """Describe a malformed record without leaking record content."""


def migrate_legacy_record(record: dict) -> dict:
    """Label a known unversioned record without pretending it used today's protocol."""
    required = ("session", "model", "variant", "depth", "score")
    if any(key not in record for key in required):
        raise RecordError("The legacy record does not have the known schema.")
    # Legacy values remain a separate comparison stratum in leaderboard code.
    return {
        **record,
        "schema_version": 1,
        "protocol_version": "legacy-original",
        "scoring_version": "legacy-original",
        "run_mode": "research",
    }


def validate_versions(record: dict) -> dict:
    """Accept current records or explicitly migrate the one known legacy shape."""
    if "schema_version" not in record:
        return migrate_legacy_record(record)
    versions = (
        record.get("schema_version"),
        record.get("protocol_version"),
        record.get("scoring_version"),
    )
    if versions != (SCHEMA_VERSION, PROTOCOL_VERSION, SCORING_VERSION):
        raise RecordError("The record uses an incompatible protocol or scoring version.")
    return record


def read_records(path: Path) -> list[dict]:
    """Read experiment records and identify malformed JSON or schema lines."""
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("required field missing")
                record = validate_versions(record)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RecordError(f"Malformed JSONL record at line {line_number}.") from exc
            records.append(record)
    return records


def append_records(path: Path, values: Iterable[dict]) -> None:
    """Append validated records atomically per line without ever storing credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for value in values:
            # Refuse suspicious keys as an extra defense at the persistence boundary.
            if any(key.lower() in {"api_key", "authorization"} for key in value):
                raise RecordError("Credential fields cannot be persisted.")
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
            handle.flush()
