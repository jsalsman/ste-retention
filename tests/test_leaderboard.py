"""Regression coverage for run-isolated factorial leaderboard calculations."""

from leaderboard import render_leaderboard


def _arm(run_id: str, variant: str, score: int) -> dict:
    """Build one minimal, valid leaderboard arm for a specified durable run."""
    # Fixed model and coordinates make only run isolation vary in these tests.
    # Minimal records keep this fixture independent from provider response details.
    return {
        "run_id": run_id,
        "session": 1,
        "model": "test-model",
        "variant": variant,
        "depth": 1,
        "score": score,
    }


def test_factorial_main_effects_average_both_levels():
    """Average each main effect over both levels of the other factor."""
    # The upper-level contrasts differ, exposing baseline-only calculations.
    records = [
        _arm("complete", variant, score)
        for variant, score in (("bare", 50), ("rules", 60), ("named", 70), ("named_rules", 90))
    ]

    rendered = render_leaderboard(records)

    # Marginal effects are rules=(10+20)/2 and naming=(20+30)/2.
    assert "+15.0" in rendered
    assert "+25.0" in rendered


def test_incomplete_retry_does_not_hide_completed_run():
    """Keep a complete retry when an earlier run checkpoint has only one arm."""
    # Both invocations reuse session/depth coordinates but carry distinct durable IDs.
    records = [_arm("failed", "bare", 40)]
    records.extend(
        _arm("retry", variant, score)
        for variant, score in (("bare", 50), ("rules", 60), ("named", 70), ("named_rules", 90))
    )

    rendered = render_leaderboard(records)

    # The complete retry contributes exactly one paired observation.
    assert "<td>1</td>" in rendered
    assert "No complete experiment records" not in rendered
