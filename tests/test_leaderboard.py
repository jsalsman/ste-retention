"""Regression coverage for run-isolated factorial leaderboard calculations."""

import re

import pytest

from ste.leaderboard import _mean_interval, render_leaderboard


def _arm(
    run_id: str,
    variant: str,
    score: int,
    *,
    session: int = 1,
    depth: int = 1,
    protocol: str = "legacy",
    scoring: str = "legacy",
) -> dict:
    """Build one minimal, valid leaderboard arm for a specified durable run."""
    # Fixed model and coordinates make only run isolation vary in these tests.
    # Minimal records keep this fixture independent from provider response details.
    return {
        "run_id": run_id,
        "session": session,
        "model": "test-model",
        "variant": variant,
        "depth": depth,
        "score": score,
        # Versions identify the compatibility stratum for displayed aggregates.
        "protocol_version": protocol,
        "scoring_version": scoring,
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
    assert "70.0" in rendered


def test_results_precede_uncertainty_in_dark_dashboard():
    """Place the prominently styled leaderboard before its interpretation guidance."""
    # A dedicated body class scopes the dark dashboard without changing the experiment form.
    # Landmark headings make the visual reordering equally clear to assistive technology.
    rendered = render_leaderboard([])

    assert '<body class="leaderboard-page">' in rendered
    assert '<h2 id="results-title">Leaderboard</h2>' in rendered
    assert rendered.index('id="results-title"') < rendered.index('id="uncertainty"')


def test_intervals_use_complete_cell_baselines_and_paired_effects():
    """Calculate baseline and all contrasts per repeated matched cell before intervals."""
    # Two repetitions in one durable coordinate exercise repeated paired observations.
    records = []
    for session, scores in enumerate(((40, 50, 35, 40), (60, 66, 54, 57)), start=1):
        records.extend(
            _arm("repeated", variant, score, session=session)
            for variant, score in zip(
                ("bare", "rules", "named", "named_rules"), scores, strict=True
            )
        )

    rendered = render_leaderboard(records)

    # Parse every estimate/limit so assertions verify numeric results, not loose substrings.
    cells = re.findall(r"<td>([+-]?\d+\.\d) ± (\d+\.\d)</td>", rendered)
    numeric = [tuple(float(value) for value in cell) for cell in cells]
    # Half-widths equal the former upper limits minus each mean.
    assert numeric == pytest.approx(
        [(44.5, 120.7), (50.0, 127.1), (6.0, 19.1), (-7.5, 0.0), (-4.0, 12.7)]
    )
    assert "<td>2</td>" in rendered


def test_mean_interval_preserves_unbounded_finite_contrasts():
    """Allow negative effects and confidence limits outside raw score bounds."""
    # Contrasts are score-point changes, so neither values nor limits are scores themselves.
    mean, lower, upper = _mean_interval((-150.0, -50.0))
    assert mean == pytest.approx(-100.0)
    assert lower == pytest.approx(-735.3102368)
    assert upper == pytest.approx(535.3102368)


def test_repeated_depths_are_clustered_within_run_session():
    """Average dependent depths so one run/session contributes one interval observation."""
    # Three complete depths mirror the continuing-history structure of research runs.
    records = []
    for depth, baseline in ((1, 20), (6, 50), (12, 80)):
        records.extend(
            _arm("one-run", variant, baseline, depth=depth)
            for variant in ("bare", "rules", "named", "named_rules")
        )

    rendered = render_leaderboard(records)

    # The session mean remains 50, but dependent depths do not create a variance estimate.
    assert "<td>50.0 ± n/a</td>" in rendered
    assert "<th>Paired sessions</th>" in rendered
    assert "<td>1</td>" in rendered


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
    assert rendered.count(" ± n/a</td>") == 5
    assert "No complete experiment records" not in rendered


def test_aggregates_remain_separate_across_protocol_and_scoring_versions():
    """Render distinct rows for complete cells with incompatible version semantics."""
    # Deliberately divergent baselines expose accidental cross-version averaging.
    records = []
    for run_id, protocol, scoring, baseline in (
        ("legacy-run", "protocol-1", "score-1", 20),
        ("current-run", "protocol-2", "score-2", 80),
    ):
        # Equal scores within each cell make every effect zero while preserving its baseline.
        records.extend(
            _arm(run_id, variant, baseline, protocol=protocol, scoring=scoring)
            for variant in ("bare", "rules", "named", "named_rules")
        )

    rendered = render_leaderboard(records)

    # Each compatibility stratum gets its own labeled row instead of a blended baseline of 50.
    assert rendered.count('data-model="test-model"') == 2
    assert "protocol-1" in rendered and "score-1" in rendered
    assert "protocol-2" in rendered and "score-2" in rendered
    assert "<td>20.0 ± n/a</td>" in rendered
    assert "<td>80.0 ± n/a</td>" in rendered
    assert "<td>50.0 ±" not in rendered


def test_incomplete_cells_do_not_enter_interval_sample():
    """Exclude an incomplete cell while retaining two complete run-level cells."""
    # Complete cells have baselines 30 and 50; the incomplete baseline 100 must not shift them.
    records = [_arm("incomplete", "bare", 100)]
    for run_id, baseline in (("one", 30), ("two", 50)):
        records.extend(
            _arm(run_id, variant, baseline) for variant in ("bare", "rules", "named", "named_rules")
        )

    rendered = render_leaderboard(records)

    baseline = re.search(r"<td>(\d+\.\d) ± (\d+\.\d)</td>", rendered)
    assert baseline is not None
    assert tuple(float(value) for value in baseline.groups()) == pytest.approx((40.0, 127.1))
    assert "<td>2</td>" in rendered


def test_rows_rank_by_named_score_without_rules_and_show_runtime():
    """Rank by the named-arm score and show total complete-run elapsed time."""
    # Model names sort opposite to their effects so this catches accidental label sorting.
    # Two runs in the winning row verify that elapsed values are added once per run.
    records = []
    for run_id, model, effect, elapsed in (
        ("one", "z-low", 5, 30),
        ("two", "a-high", 20, 70),
        ("three", "a-high", 20, 50),
    ):
        for variant, score in (
            ("bare", 40),
            ("rules", 40),
            ("named", 40 + effect),
            ("named_rules", 40 + effect),
        ):
            record = _arm(run_id, variant, score)
            record["model"] = model
            record["_run_elapsed_seconds"] = elapsed
            records.append(record)

    rendered = render_leaderboard(records)

    assert rendered.index('data-model="a-high"') < rendered.index('data-model="z-low"')
    # Rank remains ordinary tabular data while the model identifies every metric in its row.
    assert '<td>1</td><th scope="row">a-high</th><td>60.0' in rendered
    assert "<th>Protocol version</th>" not in rendered
    assert "<th>Scoring version</th>" not in rendered
    assert "<td>2m 0s</td>" in rendered


def test_ci_notation_appears_once_in_headers_and_is_explained():
    """Keep headers compact while defining the ± notation in the uncertainty section."""
    rendered = render_leaderboard([])

    # Only the first metric header names the interval; the others are bare labels.
    assert rendered.count("<th>Named without rules, mean ± 95% CI</th>") == 1
    assert "<th>Bare baseline</th>" in rendered
    assert "mean and 95% CI" not in rendered
    uncertainty = rendered[rendered.index('id="uncertainty"') :]
    assert "half-width of its 95% CI" in uncertainty


def test_chart_leads_page_and_escapes_model_names():
    """Draw one dumbbell per row above the table, escaping names and clamping limits."""
    # Two sessions give a finite but very wide interval that must be clamped to 0-100.
    records = []
    for session, (bare, named) in enumerate(((10, 30), (90, 95)), start=1):
        for variant, score in (
            ("bare", bare),
            ("rules", bare),
            ("named", named),
            ("named_rules", named),
        ):
            record = _arm("run", variant, score, session=session)
            record["model"] = "<script>x</script>"
            records.append(record)

    rendered = render_leaderboard(records)

    chart = rendered[
        rendered.index('<section class="leaderboard-chart"') : rendered.index("</svg>")
    ]
    assert rendered.index("leaderboard-chart") < rendered.index('id="results-title"')
    assert chart.count('class="dumbbell"') == 1
    assert "<script>" not in rendered
    assert "&lt;script&gt;x&lt;/script&gt;" in chart
    # Clamped intervals are disclosed with chevrons, and no coordinate leaves the plot.
    assert 'class="clip bare"' in chart
    xs = [float(value) for value in re.findall(r' (?:x|cx|x1|x2)="(-?[\d.]+)"', chart)]
    assert xs and all(0 <= value <= 800 for value in xs)


def test_chart_omitted_without_rows():
    """Leave only the table empty state when no complete records exist."""
    assert "leaderboard-chart" not in render_leaderboard([])
