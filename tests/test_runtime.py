"""Tests for active run time reconstructed from work-unit save times."""

from datetime import datetime, timedelta, timezone

import pytest

from ste.runs.runtime import PAUSE_MARGIN_SECONDS, active_run_seconds

START = datetime(2026, 10, 9, 1, 44, tzinfo=timezone.utc)


def _state(offsets, provider_timeout=120.0):
    """Build a snapshot whose units were saved at the given second offsets."""
    units = [
        {"completed_at": (START + timedelta(seconds=offset)).isoformat()} for offset in offsets
    ]
    # Only the fields the reconstruction reads are needed.
    return {
        "created_at": START.isoformat(),
        "config": {"provider_timeout": provider_timeout, "judge_timeout": 120.0},
        "units": units,
    }


def test_active_time_sums_ordinary_gaps():
    """Sum creation-to-first-save and every gap between saves when there is no pause."""
    assert active_run_seconds(_state([10, 25, 45])) == pytest.approx(45)


def test_stalled_pause_counts_as_one_typical_unit():
    """Replace a multi-hour stall with the run's median ordinary gap."""
    # Gaps: 10, 20, 20, then a 2-hour stall, then 10. Median ordinary gap is 15.
    state = _state([10, 30, 50, 50 + 7200, 50 + 7210])
    assert active_run_seconds(state) == pytest.approx(10 + 20 + 20 + 15 + 10)


def test_slow_but_legitimate_unit_is_not_a_pause():
    """Keep any gap within the unit time limit plus margin as real work."""
    slow = 120 + PAUSE_MARGIN_SECONDS
    assert active_run_seconds(_state([10, 10 + slow])) == pytest.approx(10 + slow)


@pytest.mark.parametrize(
    "state",
    [
        {"created_at": START.isoformat(), "units": []},
        {"created_at": "not a date", "units": [{"completed_at": START.isoformat()}]},
        {"created_at": START.isoformat(), "units": [{"completed_at": "2026-10-09T02:00:00"}]},
        _state([30, 10]),
    ],
)
def test_unusable_timelines_are_unavailable(state):
    """Return None for no units, bad or naive timestamps, or saves out of order."""
    assert active_run_seconds(state) is None
