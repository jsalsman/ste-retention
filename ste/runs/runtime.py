"""Active run time reconstructed from when each work unit was saved.

A run's wall-clock span includes any time it sat stalled before being resumed,
which can be hours. Every saved work unit records ``completed_at``, so the time
the run actually spent working is the sum of the gaps between consecutive
saves. A gap longer than the unit that ended it could possibly take is a pause;
it is counted as one typical unit (the median ordinary gap), because the first
unit after a resume still had to be generated.
"""

import math
import statistics
from datetime import datetime

# Extra seconds allowed beyond a unit's provider time limit before a gap counts as
# a pause. It covers checkpoint writes, lease renewal and request start-up.
PAUSE_MARGIN_SECONDS = 60.0

# The per-unit time limit used when a snapshot does not record a usable one. It
# matches the research runner's default provider and judge timeouts.
DEFAULT_UNIT_LIMIT_SECONDS = 120.0


def _timestamp(value: object) -> datetime | None:
    """Parse one saved ISO timestamp, or return ``None`` when it is unusable.

    Args:
        value: A ``created_at`` or ``completed_at`` value read from a snapshot.

    Returns:
        A timezone-aware datetime, or ``None`` for non-strings, unparseable text,
        and naive timestamps.

    """
    if not isinstance(value, str):
        # Missing or non-text values cannot anchor a timeline.
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Malformed text is treated like a missing timestamp.
        return None
    # Naive values cannot be compared with timezone-aware ones, so they are unusable.
    return parsed if parsed.tzinfo is not None else None


def _limit(config: object, key: str) -> float:
    """Return one configured per-unit time limit, or the default when unusable.

    Args:
        config: The snapshot's ``config`` mapping, if any.
        key: ``"provider_timeout"`` for generations or ``"judge_timeout"`` for judges.

    Returns:
        The configured finite positive limit in seconds, else the default.

    """
    value = config.get(key) if isinstance(config, dict) else None
    # Booleans are numbers in Python, but never a meaningful time limit.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Only finite positive numbers are real limits.
        if math.isfinite(value) and value > 0:
            return float(value)
    return DEFAULT_UNIT_LIMIT_SECONDS


def active_run_seconds(state: dict) -> float | None:
    """Return the seconds a research run spent working, excluding stalled pauses.

    Each gap ends with one saved unit. A judge unit may take up to the judge
    timeout and any other unit up to the provider timeout; a gap longer than that
    limit plus :data:`PAUSE_MARGIN_SECONDS` is a pause.

    Args:
        state: A validated research snapshot with ``created_at`` and saved units.

    Returns:
        The reconstructed active time, or ``None`` when the snapshot has no units
        or any timestamp is missing, malformed, or out of order.

    """
    units = state.get("units")
    start = _timestamp(state.get("created_at"))
    if start is None or not isinstance(units, list) or not units:
        # Without a start and saved units there is no timeline to measure.
        return None
    config = state.get("config")
    generation_limit = _limit(config, "provider_timeout")
    judge_limit = _limit(config, "judge_timeout")
    previous = start
    gaps: list[tuple[float, float]] = []
    for unit in units:
        stamp = _timestamp(unit.get("completed_at") if isinstance(unit, dict) else None)
        if stamp is None:
            # One unusable timestamp makes the reconstruction untrustworthy.
            return None
        gap = (stamp - previous).total_seconds()
        # Units are appended as they are saved, so their times never go backwards.
        if not math.isfinite(gap) or gap < 0:
            return None
        is_judge = isinstance(unit, dict) and unit.get("kind") == "judge"
        # The unit that ended the gap decides how long its work could have taken.
        limit = judge_limit if is_judge else generation_limit
        gaps.append((gap, limit + PAUSE_MARGIN_SECONDS))
        previous = stamp
    ordinary = [gap for gap, pause_after in gaps if gap <= pause_after]
    # A pause still contains one unit's real work, estimated as a typical unit.
    typical = statistics.median(ordinary) if ordinary else generation_limit
    return sum(gap if gap <= pause_after else typical for gap, pause_after in gaps)
