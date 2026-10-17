"""Active run time reconstructed from when each work unit was saved.

A run's wall-clock span includes any time it sat stalled before being resumed,
which can be hours. Every saved work unit records ``completed_at``, so the time
the run actually spent working is the sum of the gaps between consecutive
saves. A gap longer than one unit could possibly take is a pause; it is counted
as one typical unit (the median ordinary gap), because the first unit after a
resume still had to be generated.
"""

import math
import statistics
from datetime import datetime

# Extra seconds allowed beyond a unit's provider time limit before a gap counts as
# a pause. It covers checkpoint writes, lease renewal and request start-up.
PAUSE_MARGIN_SECONDS = 60.0

# The limit used when a snapshot does not record its timeouts.
DEFAULT_UNIT_LIMIT_SECONDS = 120.0


def _timestamp(value: object) -> datetime | None:
    """Parse one saved ISO timestamp, or return ``None`` when it is unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Naive values cannot be compared with timezone-aware ones, so they are unusable.
    return parsed if parsed.tzinfo is not None else None


def _unit_limit(state: dict) -> float:
    """Return the longest time one work unit can legitimately take, in seconds."""
    config = state.get("config")
    limits = []
    for key in ("provider_timeout", "judge_timeout"):
        value = config.get(key) if isinstance(config, dict) else None
        # Only finite positive numbers are real limits; anything else is ignored.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and value > 0:
                limits.append(float(value))
    return max(limits, default=DEFAULT_UNIT_LIMIT_SECONDS)


def active_run_seconds(state: dict) -> float | None:
    """Return the seconds a research run spent working, excluding stalled pauses.

    Args:
        state: A validated research snapshot with ``created_at`` and saved units.

    Returns:
        The reconstructed active time, or ``None`` when the snapshot has no units
        or any timestamp is missing, malformed, or out of order.

    """
    units = state.get("units")
    start = _timestamp(state.get("created_at"))
    if start is None or not isinstance(units, list) or not units:
        return None
    times = []
    for unit in units:
        stamp = _timestamp(unit.get("completed_at") if isinstance(unit, dict) else None)
        if stamp is None:
            # One unusable timestamp makes the reconstruction untrustworthy.
            return None
        times.append(stamp)
    # Units are appended as they are saved, so their times must never go backwards.
    pairs = zip([start, *times[:-1]], times, strict=True)
    gaps = [(later - earlier).total_seconds() for earlier, later in pairs]
    if any(not math.isfinite(gap) or gap < 0 for gap in gaps):
        return None
    pause_after = _unit_limit(state) + PAUSE_MARGIN_SECONDS
    ordinary = [gap for gap in gaps if gap <= pause_after]
    # A pause still contains one unit's real work, estimated as a typical unit.
    typical = statistics.median(ordinary) if ordinary else pause_after
    return sum(gap if gap <= pause_after else typical for gap in gaps)
