"""Safe pure rendering functions for experiment leaderboards."""

import html
import math
import statistics

from records import read_records


def _number(value: object) -> float:
    """Return a finite chart-safe number or reject the malformed record value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # SVG never receives strings or non-finite coordinates.
        raise ValueError("Leaderboard scores must be numeric.")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError("Leaderboard scores must be finite values from 0 to 100.")
    return result


def render_leaderboard(records: list[dict], *, synthetic: bool = False) -> str:
    """Render paired four-arm effects without collapsing the experiment variants."""
    # A comparison is valid only within the same model, session, and conversation depth.
    paired: dict[tuple[str, object, object], dict[str, list[float]]] = {}
    for record in records:
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
        # Repeated arms represent separate completed runs in durable aggregate storage.
        arms = paired.setdefault((model, session, depth), {})
        arms.setdefault(variant, []).append(_number(record.get("score")))

    # Each tuple contains baseline, rule-detail, naming, and interaction effects.
    grouped: dict[str, list[tuple[float, float, float, float]]] = {}
    for (model, _session, _depth), arms in paired.items():
        counts = {len(values) for values in arms.values()}
        if len(arms) != 4 or len(counts) != 1:
            # Never compare unmatched responses from different sessions or depths.
            continue
        for bare, rules, named, named_rules in zip(
            arms["bare"], arms["rules"], arms["named"], arms["named_rules"], strict=True
        ):
            rule_effect = rules - bare
            naming_effect = named - bare
            interaction = named_rules - named - rules + bare
            grouped.setdefault(model, []).append((bare, rule_effect, naming_effect, interaction))
    rows = []
    for model, contrasts in sorted(grouped.items()):
        # Both displayed text and data attributes are escaped from external records.
        safe_model = html.escape(model, quote=True)
        means = [statistics.mean(values) for values in zip(*contrasts, strict=True)]
        rows.append(
            f'<tr data-model="{safe_model}"><th scope="row">{safe_model}</th>'
            f"<td>{means[0]:.1f}</td><td>{means[1]:+.1f}</td>"
            f"<td>{means[2]:+.1f}</td><td>{means[3]:+.1f}</td>"
            f"<td>{len(contrasts)}</td></tr>"
        )
    label = "Synthetic preview — not experimental data" if synthetic else "Experiment results"
    empty = '<p class="empty">No complete experiment records are available.</p>' if not rows else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(label)}</title>"
        '<link rel="stylesheet" href="/static/styles.css"></head>'
        f'<body><main><p><a href="/">Back to experiment</a></p><h1>{html.escape(label)}</h1>{empty}'
        "<table><caption>Paired deterministic compliance-score contrasts.</caption>"
        "<thead><tr><th>Model</th><th>Bare baseline</th><th>Rule effect</th>"
        "<th>Naming effect</th><th>Interaction</th><th>Paired observations</th></tr></thead>"
        f"<tbody>{''.join(rows)}"
        "</tbody></table></main></body></html>"
    )


def render_file(path) -> str:
    """Read a JSONL path and return its safely rendered standalone leaderboard."""
    # Keeping file access outside rendering makes the latter easy to test.
    return render_leaderboard(read_records(path))
