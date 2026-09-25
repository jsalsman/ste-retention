"""Safe pure rendering functions for experiment leaderboards."""

import html
import math
import statistics

from ste.records import read_records
from ste.statistics import paired_t


# These labels are explanatory rather than abbreviated statistical jargon. They are
# rendered as a definition list so screen-reader and sighted users get the same context.
METRIC_DEFINITIONS = (
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

    The dependency-free paired-t helper supplies its documented normal-critical-value
    half-width (1.96 standard errors). A single complete cell has a valid point estimate
    but cannot supply a variance estimate, so both confidence limits are ``None``.
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
    """Format one estimate and its associated interval as unambiguous table text."""
    mean, lower, upper = summary
    # Contrasts always show a sign; baseline scores retain their familiar score format.
    format_spec = "+.1f" if signed else ".1f"
    estimate = format(mean, format_spec)
    if lower is None or upper is None:
        # The visible unavailable label prevents a one-cell row implying zero uncertainty.
        return f"{estimate} (95% CI unavailable; fewer than 2 observations)"
    return f"{estimate} (95% CI {format(lower, format_spec)} to {format(upper, format_spec)})"


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

    # Each versioned group contains baseline, rule-detail, naming, and interaction effects.
    grouped: dict[tuple[str, object, object], list[tuple[float, float, float, float]]] = {}
    for (model, _run_id, _session, _depth, protocol, scoring), arms in paired.items():
        counts = {len(values) for values in arms.values()}
        if len(arms) != 4 or len(counts) != 1:
            # Never compare unmatched responses from different sessions or depths.
            continue
        for bare, rules, named, named_rules in zip(
            arms["bare"], arms["rules"], arms["named"], arms["named_rules"], strict=True
        ):
            # Main effects average the simple contrast across both levels of the other factor.
            rule_effect = ((rules - bare) + (named_rules - named)) / 2
            naming_effect = ((named - bare) + (named_rules - rules)) / 2
            interaction = named_rules - named - rules + bare
            # Preserve both compatibility dimensions when aggregating complete cells.
            grouped.setdefault((model, protocol, scoring), []).append(
                (bare, rule_effect, naming_effect, interaction)
            )
    rows = []
    for (model, protocol, scoring), contrasts in sorted(
        grouped.items(), key=lambda item: tuple(str(value) for value in item[0])
    ):
        # Both displayed text and data attributes are escaped from external records.
        safe_model = html.escape(model, quote=True)
        safe_protocol = html.escape(str(protocol), quote=True)
        safe_scoring = html.escape(str(scoring), quote=True)
        # Summarize already paired cell-level values rather than four independent arms.
        summaries = [_mean_interval(values) for values in zip(*contrasts, strict=True)]
        cells = [
            _estimate_cell(summary, signed=index > 0) for index, summary in enumerate(summaries)
        ]
        rows.append(
            f'<tr data-model="{safe_model}"><th scope="row">{safe_model}</th>'
            f"<td>{safe_protocol}</td><td>{safe_scoring}</td>"
            f"<td>{cells[0]}</td><td>{cells[1]}</td>"
            f"<td>{cells[2]}</td><td>{cells[3]}</td>"
            f"<td>{len(contrasts)}</td></tr>"
        )
    label = "Synthetic preview — not experimental data" if synthetic else "Experiment results"
    empty = '<p class="empty">No complete experiment records are available.</p>' if not rows else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(label)}</title>"
        '<link rel="stylesheet" href="/static/styles.css"></head>'
        f'<body><main><p><a href="/">Back to experiment</a></p><h1>{html.escape(label)}</h1>'
        '<section aria-labelledby="about-results"><h2 id="about-results">About the experiment</h2>'
        "<p>The four variants are <strong>bare</strong> (prompt alone), <strong>rules</strong> "
        "(rules spelled out), <strong>named</strong> (technique named), and "
        "<strong>named_rules</strong> (name and rules together). Compliance scores run from "
        "0 to 100; higher scores mean greater compliance. Effects are paired changes in "
        "score points.</p>"
        "<dl>"
        + "".join(
            f"<dt>{term}</dt><dd>{description}</dd>" for term, description in METRIC_DEFINITIONS
        )
        + "</dl><p>A negative interaction may reflect overlap between naming the technique and "
        "spelling out its rules, but the interaction does not identify its cause.</p></section>"
        '<section aria-labelledby="uncertainty"><h2 id="uncertainty">Understanding uncertainty</h2>'
        "<p>A 95% confidence interval (95% CI) is the estimate plus or minus 1.96 standard errors "
        "across contributing complete cells. Wider intervals indicate greater uncertainty. It is "
        "neither the range of individual scores nor a 95% probability that the fixed population "
        "effect lies inside this observed interval. Interpret estimates and uncertainty together, "
        "rather than reducing them to statistically significant or not significant.</p>"
        "<p>Only complete four-arm research cells matched within run, model, session, depth, "
        "protocol version, and scoring version are included; previews and incomplete runs are "
        "excluded. Effects are calculated within each matched cell before their means and "
        "intervals. "
        "The interval treats complete cells as independent, not as a clustered or hierarchical "
        "analysis; pooling across sessions, depths, or runs may reflect variation at more than "
        "one level.</p>"
        f'</section>{empty}<div class="table-scroll" tabindex="0" '
        'aria-label="Scrollable experiment results table"><table>'
        "<caption>Mean compliance scores and paired score-point effects with two-sided 95% "
        "confidence intervals.</caption>"
        "<thead><tr><th>Model</th><th>Protocol version</th><th>Scoring version</th>"
        "<th>Bare baseline, mean and 95% CI</th><th>Rule effect, mean and 95% CI</th>"
        "<th>Naming effect, mean and 95% CI</th><th>Interaction, mean and 95% CI</th>"
        "<th>Paired observations</th></tr></thead>"
        f"<tbody>{''.join(rows)}"
        "</tbody></table></div></main></body></html>"
    )


def render_file(path) -> str:
    """Read a JSONL path and return its safely rendered standalone leaderboard."""
    # Keeping file access outside rendering makes the latter easy to test.
    return render_leaderboard(read_records(path))
