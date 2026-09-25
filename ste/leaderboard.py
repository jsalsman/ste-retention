"""Safe pure rendering functions for experiment leaderboards."""

import html
import math
import statistics

from ste.records import read_records
from ste.statistics import paired_t


# These labels are explanatory rather than abbreviated statistical jargon. They are
# rendered as a definition list so screen-reader and sighted users get the same context.
METRIC_DEFINITIONS = (
    (
        "Named without rules",
        "Mean compliance score when ASD-STE100 is named without spelling out its rules.",
    ),
    ("Bare baseline", "Mean score for the bare prompt, before naming or rules are added."),
    (
        "Rule effect",
        "Mean paired change from spelling out the rules, averaged with and without naming.",
    ),
    (
        "Naming effect",
        "Mean paired change from naming the technique, averaged with and without rules.",
    ),
    (
        "Interaction",
        "The extra paired change when naming and rules appear together, "
        "beyond their separate effects.",
    ),
    (
        "Paired observations",
        "Complete four-arm research cells used to calculate every value in the row.",
    ),
)


def _number(value: object) -> float:
    """Return a finite chart-safe number or reject the malformed record value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # SVG never receives strings or non-finite coordinates.
        raise ValueError("Leaderboard scores must be numeric.")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError("Leaderboard scores must be finite values from 0 to 100.")
    return result


def _mean_interval(values: tuple[float, ...]) -> tuple[float, float | None, float | None]:
    """Return a finite mean and two-sided 95% CI for complete-cell measurements.

    The dependency-free paired-t helper supplies a Student-t half-width using the
    available degrees of freedom. A single independent session has a valid point
    estimate but cannot supply a variance estimate, so both limits are ``None``.
    """
    # Inputs are derived only from finite validated scores, but check every boundary so
    # malformed intermediate values can never reach HTML formatting unnoticed.
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("Leaderboard summaries require finite complete-cell values.")
    mean = statistics.mean(values)
    result = paired_t(list(values))
    if result is None:
        # One observation supports the displayed point, not an uncertainty interval.
        return mean, None, None
    half_width = result[-1]
    lower, upper = mean - half_width, mean + half_width
    if not all(math.isfinite(value) for value in (mean, lower, upper)):
        # Confidence limits and estimates must remain safe for direct text rendering.
        raise ValueError("Leaderboard confidence intervals must be finite.")
    return mean, lower, upper


def _estimate_cell(summary: tuple[float, float | None, float | None], *, signed: bool) -> str:
    """Format one estimate and its 95% CI half-width as compact ``mean ± half`` text.

    The interval is symmetric around the Student-t mean, so the half-width fully
    describes it. The uncertainty section explains the notation once, which keeps
    column headers and cells short.
    """
    mean, lower, upper = summary
    # Contrasts always show a sign; baseline scores retain their familiar score format.
    estimate = format(mean, "+.1f" if signed else ".1f")
    if lower is None or upper is None:
        # The visible n/a prevents a one-session row implying zero uncertainty.
        return f"{estimate} ± n/a"
    # Half-widths are unsigned distances, even when the estimate itself is signed.
    return f"{estimate} ± {format(upper - mean, '.1f')}"


def _elapsed_cell(run_seconds: dict[object, float], expected_runs: set[object]) -> str:
    """Format total elapsed wall time only when every contributing run has safe metadata."""
    # Partial timing would understate the workload, so mixed historical/current rows
    # receive the same explicit unavailable treatment as fully historical rows.
    if not expected_runs.issubset(run_seconds):
        return "Unavailable"
    total_seconds = round(sum(run_seconds[run_id] for run_id in expected_runs))
    # A compact duration remains readable beside wide confidence-interval columns.
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# Chart geometry is in SVG user units; the viewBox scales it to the container width.
# Each model gets a label line above its dumbbell so long identifiers never need a
# fixed left gutter, which keeps the figure legible on phone-width screens.
CHART_WIDTH = 800
CHART_PAD_X = 16
CHART_PAD_TOP = 8
CHART_ROW_HEIGHT = 52
CHART_AXIS_HEIGHT = 34
# Compliance scores are bounded, so the shared axis covers the full 0-100 scale.
CHART_TICKS = (0, 25, 50, 75, 100)


def _chart_x(score: float) -> float:
    """Map a score to a clamped, validated SVG x coordinate on the 0-100 axis."""
    # Every chart coordinate passes through here, so non-finite values never reach SVG.
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("Leaderboard chart values must be numeric.")
    if not math.isfinite(score):
        raise ValueError("Leaderboard chart values must be finite.")
    # Wide small-sample intervals can extend past the score scale; clamp them to the plot.
    clamped = min(100.0, max(0.0, float(score)))
    return CHART_PAD_X + clamped / 100 * (CHART_WIDTH - 2 * CHART_PAD_X)


def _chart_band(summary: tuple[float, float | None, float | None], y: float, css_class: str) -> str:
    """Return one translucent 95% CI band, with edge ticks where it leaves the axis."""
    _mean, lower, upper = summary
    if lower is None or upper is None:
        # A single session has no interval, so only its point marker is drawn.
        return ""
    left, right = _chart_x(lower), _chart_x(upper)
    # Thin rounded bands stay behind the markers and read as uncertainty, not data bars.
    band = (
        f'<rect class="band {css_class}" x="{left:.2f}" y="{y - 2.5:.2f}" '
        f'width="{max(right - left, 1):.2f}" height="5" rx="2.5"/>'
    )
    # Small chevrons disclose that the interval continues beyond the visible scale.
    if lower < 0:
        band += (
            f'<path class="clip {css_class}" d="M{left + 6:.2f} {y - 5:.2f} '
            f'L{left:.2f} {y:.2f} L{left + 6:.2f} {y + 5:.2f}"/>'
        )
    if upper > 100:
        band += (
            f'<path class="clip {css_class}" d="M{right - 6:.2f} {y - 5:.2f} '
            f'L{right:.2f} {y:.2f} L{right - 6:.2f} {y + 5:.2f}"/>'
        )
    return band


def _render_chart(
    chart_rows: list[tuple[int, str, tuple, tuple]],
) -> str:
    """Return a dumbbell chart linking each row's bare baseline to its named score.

    ``chart_rows`` holds ``(rank, model, named_summary, bare_summary)`` tuples in table
    order. Each summary is ``(mean, lower, upper)`` from :func:`_mean_interval`. The
    connecting line shows the named-minus-bare change; translucent bands show each
    mean's 95% CI. The table below remains the full accessible data view.
    """
    if not chart_rows:
        # The table already carries the empty-state message, so no empty axis is drawn.
        return ""
    height = CHART_PAD_TOP + len(chart_rows) * CHART_ROW_HEIGHT + CHART_AXIS_HEIGHT
    plot_bottom = height - CHART_AXIS_HEIGHT
    # Recessive gridlines and tick labels share one axis for both series.
    grid = "".join(
        f'<line class="grid" x1="{_chart_x(tick):.2f}" y1="{CHART_PAD_TOP}" '
        f'x2="{_chart_x(tick):.2f}" y2="{plot_bottom}"/>'
        f'<text class="tick" x="{_chart_x(tick):.2f}" y="{plot_bottom + 20}" '
        f'text-anchor="{"start" if tick == 0 else "end" if tick == 100 else "middle"}">'
        f"{tick}</text>"
        for tick in CHART_TICKS
    )
    rows = []
    for index, (rank, model, named, bare) in enumerate(chart_rows):
        top = CHART_PAD_TOP + index * CHART_ROW_HEIGHT
        # Label text sits above the dumbbell; model names were escaped by the caller.
        label_y, mark_y = top + 17, top + 36
        named_x, bare_x = _chart_x(named[0]), _chart_x(bare[0])
        # Native SVG titles provide the hover readout without client-side script.
        tooltip = html.escape(
            f"{rank}. {model}: bare {bare[0]:.1f}, named {named[0]:.1f} "
            f"({named[0] - bare[0]:+.1f})",
            quote=False,
        )
        rows.append(
            f'<g class="dumbbell"><title>{tooltip}</title>'
            # A transparent hit target makes the whole row hoverable, not just the marks.
            f'<rect class="hit" x="0" y="{top}" width="{CHART_WIDTH}" '
            f'height="{CHART_ROW_HEIGHT}"/>'
            f'<text class="label" x="{CHART_PAD_X}" y="{label_y}">'
            f'<tspan class="rank">{rank}.</tspan> {html.escape(model, quote=False)}</text>'
            # Bands render first so the connector and markers stay crisp above them.
            f"{_chart_band(bare, mark_y - 4, 'bare')}{_chart_band(named, mark_y + 4, 'named')}"
            f'<line class="connector" x1="{bare_x:.2f}" y1="{mark_y}" '
            f'x2="{named_x:.2f}" y2="{mark_y}"/>'
            # Hollow versus filled markers keep the series distinct without color.
            f'<circle class="point bare" cx="{bare_x:.2f}" cy="{mark_y}" r="5"/>'
            f'<circle class="point named" cx="{named_x:.2f}" cy="{mark_y}" r="5.5"/></g>'
        )
    # The legend is HTML so its text wraps and inherits page typography.
    legend = (
        '<ul class="chart-legend">'
        '<li><span class="swatch bare" aria-hidden="true"></span>Bare baseline</li>'
        '<li><span class="swatch named" aria-hidden="true"></span>Named without rules</li>'
        '<li><span class="swatch band" aria-hidden="true"></span>95% confidence interval</li>'
        "</ul>"
    )
    return (
        '<section class="leaderboard-chart" aria-labelledby="chart-title">'
        '<div class="section-heading"><p class="eyebrow">At a glance</p>'
        '<h2 id="chart-title">Naming versus bare baseline</h2></div>'
        # The summary sentence gives screen-reader users the chart's purpose up front.
        '<p id="chart-summary">Each line joins a model\'s bare-prompt mean compliance score to '
        "its mean when ASD-STE100 is named without rules. Shaded bands show 95% confidence "
        "intervals; chevrons mark bands that extend past the 0 to 100 scale. The table below "
        f"lists the exact values.</p>{legend}"
        # Like the table, a focusable scroll region keeps narrow screens legible.
        '<div class="chart-scroll" tabindex="0" aria-label="Scrollable naming versus bare chart">'
        f'<svg class="dumbbell-chart" viewBox="0 0 {CHART_WIDTH} {height}" role="img" '
        'aria-labelledby="chart-title" aria-describedby="chart-summary" '
        f'preserveAspectRatio="xMidYMin meet">{grid}{"".join(rows)}</svg></div></section>'
    )


def _render_explanation() -> tuple[str, str]:
    """Return separate study-context and uncertainty sections for flexible page ordering."""
    # Definition markup is static and the global definitions contain only trusted text.
    definitions = "".join(
        f"<dt>{term}</dt><dd>{description}</dd>" for term, description in METRIC_DEFINITIONS
    )
    # Keep study-design details adjacent to the metric definitions they qualify.
    experiment = (
        '<section aria-labelledby="about-results"><h2 id="about-results">About the experiment</h2>'
        # Spell out variant names rather than relying on internal identifiers alone.
        "<p>The four variants are <strong>bare</strong> (prompt alone), <strong>rules</strong> "
        "(rules spelled out), <strong>named</strong> (technique named), and "
        # Define the scale before presenting effect estimates in score points.
        "<strong>named_rules</strong> (name and rules together). Compliance scores run from "
        "0 to 100; higher scores mean greater compliance. Effects are paired changes in "
        f"score points.</p><dl>{definitions}</dl>"
        # Interaction signs alone do not establish a causal explanation.
        "<p>A negative interaction may reflect overlap between naming the technique and "
        "spelling out its rules, but the interaction does not identify its cause.</p></section>"
    )
    # Explain the sampling unit explicitly so repeated depths are not mistaken for replicates.
    uncertainty = (
        '<section aria-labelledby="uncertainty"><h2 id="uncertainty">Understanding uncertainty</h2>'
        # Name the finite-sample reference distribution used by the numeric calculation.
        "<p>A 95% confidence interval (95% CI) uses the Student t critical value for the "
        "number of contributing sessions. Wider intervals indicate greater uncertainty. It is "
        # Guard against two common probability and raw-range interpretations of an interval.
        "neither the range of individual scores nor a 95% probability that the fixed population "
        "effect lies inside this observed interval. Interpret estimates and uncertainty together, "
        "rather than reducing them to statistically significant or not significant.</p>"
        # The table writes intervals compactly, so the notation is defined once here.
        "<p>In the leaderboard table, each value is written as a mean ± the half-width of its "
        "95% CI, so the interval runs from the mean minus that amount to the mean plus it. "
        "± n/a marks rows with fewer than 2 sessions, which cannot support an interval.</p>"
        # State cell eligibility before describing the later clustering operation.
        "<p>Only complete four-arm research cells matched within run, model, session, depth, "
        "protocol version, and scoring version are included; previews and incomplete runs are "
        # Repeated depths share history and therefore become one run/session observation.
        "excluded. Effects are calculated within each matched cell, then repeated depths within "
        "each run and session are averaged before intervals are calculated. Sessions are treated "
        # The remaining independence assumption is visible rather than implied.
        "as independent; this is not a hierarchical analysis across runs or other "
        "levels.</p></section>"
    )
    # Returning separate blocks lets the results lead directly into their interpretation.
    # The ordering remains explicit at the document assembly point below.
    return experiment, uncertainty


def _render_table(rows: list[str]) -> str:
    """Return the keyboard-scrollable results table and its empty-state message."""
    # An explicit empty message remains visible while the table preserves a stable structure.
    empty = '<p class="empty">No complete experiment records are available.</p>' if not rows else ""
    # The focusable wrapper lets keyboard users reach columns outside narrow viewports.
    return (
        f'{empty}<div class="table-scroll" tabindex="0" '
        # Name the scroll region independently from the table caption.
        'aria-label="Scrollable experiment results table"><table>'
        # The caption gives the unit and interval type before users traverse columns.
        "<caption>Mean compliance scores and paired score-point effects, each ± its two-sided "
        '95% confidence interval half-width (see <a href="#uncertainty">Understanding '
        "uncertainty</a>).</caption>"
        # Compatibility columns explain why the same model can occupy multiple rows.
        "<thead><tr><th>Rank</th><th>Model</th>"
        "<th>Named without rules, mean ± 95% CI</th>"
        # The first metric header states the notation once; the rest stay compact.
        "<th>Bare baseline</th><th>Rule effect</th>"
        "<th>Naming effect</th><th>Interaction</th>"
        # Count the independent clustered units rather than the underlying depth cells.
        "<th>Paired sessions</th><th>Run elapsed</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def render_leaderboard(records: list[dict], *, synthetic: bool = False) -> str:
    """Render run-isolated, paired factorial effects for complete four-arm groups."""
    # Legacy files lack run IDs, so repeated coordinates delimit successive CLI runs.
    legacy_state: dict[str, tuple[int, set[tuple[object, object, object]]]] = {}
    # Comparisons are valid only within one model, run, session, and conversation depth.
    paired: dict[tuple[str, object, object, object, object, object], dict[str, list[float]]] = {}
    for record in records:
        if record.get("run_mode") == "preview" and not synthetic:
            # Interactive demonstrations are never silently pooled into research.
            continue
        model = str(record.get("model", "Unknown model"))
        session = record.get("session")
        depth = record.get("depth")
        if isinstance(session, bool) or not isinstance(session, (int, str)):
            raise ValueError("Leaderboard sessions must be integer or string identifiers.")
        if isinstance(depth, bool) or not isinstance(depth, (int, str)):
            raise ValueError("Leaderboard depths must be integer or string identifiers.")
        variant = record.get("variant")
        if variant not in {"bare", "rules", "named", "named_rules"}:
            raise ValueError("Leaderboard records contain an unknown experiment variant.")
        run_id = record.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise ValueError("Leaderboard run identifiers must be strings when present.")
        if run_id is None:
            # Preserve old JSONL support while preventing a restarted sequence from merging.
            generation, seen = legacy_state.setdefault(model, (0, set()))
            coordinate = (session, variant, depth)
            if coordinate in seen:
                # A repeated coordinate starts a new inferred legacy generation.
                generation, seen = generation + 1, set()
            seen.add(coordinate)
            # Store the reset set so later records join only this generation.
            legacy_state[model] = (generation, seen)
            run_id = ("legacy", generation)
        # Repeated arms can still represent replicated observations within an explicit run.
        protocol = record.get("protocol_version", "legacy")
        scoring = record.get("scoring_version", "legacy")
        arms = paired.setdefault((model, run_id, session, depth, protocol, scoring), {})
        arms.setdefault(variant, []).append(_number(record.get("score")))

    # Cells first collect within their independent run/session sampling unit.
    clustered: dict[
        tuple[str, object, object, object, object], list[tuple[float, float, float, float, float]]
    ] = {}
    # Timing belongs to a complete parent run rather than one response record. Values
    # are deduplicated by run below so repeated sessions and depths cannot inflate them.
    elapsed_by_run: dict[object, float] = {}
    for (model, run_id, session, _depth, protocol, scoring), arms in paired.items():
        counts = {len(values) for values in arms.values()}
        if len(arms) != 4 or len(counts) != 1:
            # Never compare unmatched responses from different sessions or depths.
            continue
        for bare, rules, named, named_rules in zip(
            arms["bare"], arms["rules"], arms["named"], arms["named_rules"], strict=True
        ):
            # The requested ranking value is the named-arm outcome without added rules.
            named_without_rules = bare + (named - bare)
            # Main effects average the simple contrast across both levels of the other factor.
            rule_effect = ((rules - bare) + (named_rules - named)) / 2
            naming_effect = ((named - bare) + (named_rules - rules)) / 2
            interaction = named_rules - named - rules + bare
            # Repeated depths and retries in one session are dependent, so cluster them.
            clustered.setdefault((model, run_id, session, protocol, scoring), []).append(
                (named_without_rules, bare, rule_effect, naming_effect, interaction)
            )
        # Timing metadata is identical across records copied from one completed snapshot.
        # Validate it again because JSONL and direct callers can supply arbitrary records.
        run_elapsed = next(
            (
                record.get("_run_elapsed_seconds")
                for record in records
                if record.get("run_id") == run_id
            ),
            None,
        )
        if isinstance(run_elapsed, (int, float)) and not isinstance(run_elapsed, bool):
            numeric_elapsed = float(run_elapsed)
            if math.isfinite(numeric_elapsed) and numeric_elapsed >= 0:
                elapsed_by_run[run_id] = numeric_elapsed
    # Average each cluster into one observation before estimating sampling uncertainty.
    grouped: dict[tuple[str, object, object], list[tuple[float, float, float, float, float]]] = {}
    grouped_runs: dict[tuple[str, object, object], set[object]] = {}
    for (model, _run_id, _session, protocol, scoring), cells in clustered.items():
        # Column-wise means give baseline and three effects equal session-level weight.
        session_summary = tuple(statistics.mean(values) for values in zip(*cells, strict=True))
        group_key = (model, protocol, scoring)
        grouped.setdefault(group_key, []).append(session_summary)
        grouped_runs.setdefault(group_key, set()).add(_run_id)
    rows = []
    # Chart rows mirror table order so rank labels agree across both views.
    chart_rows = []
    summaries_by_group = {
        key: [_mean_interval(values) for values in zip(*contrasts, strict=True)]
        for key, contrasts in grouped.items()
    }
    # Highest named-without-rules score leads the table; stable labels break equal-score ties.
    ordered_groups = sorted(
        grouped,
        key=lambda key: (-summaries_by_group[key][0][0], *(str(value) for value in key)),
    )
    for rank, (model, protocol, scoring) in enumerate(ordered_groups, start=1):
        group_key = (model, protocol, scoring)
        contrasts = grouped[group_key]
        # Both displayed text and data attributes are escaped from external records.
        safe_model = html.escape(model, quote=True)
        safe_protocol = html.escape(str(protocol), quote=True)
        safe_scoring = html.escape(str(scoring), quote=True)
        # Summarize session clusters rather than treating repeated depths as independent.
        summaries = summaries_by_group[group_key]
        # Factorial effects show direction; the two observed score means are unsigned.
        # The positional flags mirror the tuple assembled for each complete cell above.
        cells = [
            _estimate_cell(summary, signed=index > 1) for index, summary in enumerate(summaries)
        ]
        # The chart receives raw text; it escapes model names itself for SVG output.
        chart_rows.append((rank, model, summaries[0], summaries[1]))
        rows.append(
            f'<tr data-model="{safe_model}" data-protocol="{safe_protocol}" '
            f'data-scoring="{safe_scoring}">'
            f'<td>{rank}</td><th scope="row">{safe_model}</th>'
            f"<td>{cells[0]}</td><td>{cells[1]}</td><td>{cells[2]}</td>"
            f"<td>{cells[3]}</td><td>{cells[4]}</td>"
            f"<td>{len(contrasts)}</td>"
            f"<td>{_elapsed_cell(elapsed_by_run, grouped_runs[group_key])}</td></tr>"
        )
    label = "Synthetic preview — not experimental data" if synthetic else "Experiment results"
    # Keep the primary leaderboard above the supporting uncertainty detail so visitors
    # encounter the requested results immediately after learning what each metric means.
    experiment, uncertainty = _render_explanation()
    # The outer document stays standalone while focused helpers build its substantial blocks.
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(label)}</title>"
        '<link rel="stylesheet" href="/static/styles.css"></head>'
        '<body class="leaderboard-page"><main class="leaderboard-shell">'
        '<a class="back-link" href="/">← Back to experiment</a>'
        '<header class="leaderboard-hero"><p class="eyebrow">Research dashboard</p>'
        f'<h1>{html.escape(label)}</h1><p class="leaderboard-intro">Compare controlled-language '
        "prompt strategies with paired effects and confidence intervals.</p></header>"
        # The overview chart leads the page; the detailed table follows the definitions.
        f"{_render_chart(chart_rows)}"
        f'{experiment}<section class="leaderboard-results" aria-labelledby="results-title">'
        '<div class="section-heading"><p class="eyebrow">Measured outcomes</p>'
        '<h2 id="results-title">Leaderboard</h2></div>'
        f"{_render_table(rows)}</section>{uncertainty}</main></body></html>"
    )


def render_file(path) -> str:
    """Read a JSONL path and return its safely rendered standalone leaderboard."""
    # Keeping file access outside rendering makes the latter easy to test.
    return render_leaderboard(read_records(path))
