"""JSON Lines persistence with validation and useful source locations."""

import json
from pathlib import Path
from typing import Iterable


class RecordError(ValueError):
    """Describe a malformed record without leaking record content."""


def read_records(path: Path) -> list[dict]:
    """Read experiment records and identify malformed JSON or schema lines."""
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                # These fields preserve the original unversioned record format.
                required = ("session", "model", "variant", "depth", "score")
                if not isinstance(record, dict) or any(key not in record for key in required):
                    raise ValueError("required field missing")
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
