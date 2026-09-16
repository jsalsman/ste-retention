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
    """Return a standalone escaped HTML leaderboard from validated record dictionaries."""
    grouped: dict[str, list[float]] = {}
    for record in records:
        model = str(record.get("model", "Unknown model"))
        grouped.setdefault(model, []).append(_number(record.get("score")))
    rows = []
    for model, scores in sorted(
        grouped.items(), key=lambda item: statistics.mean(item[1]), reverse=True
    ):
        # Both displayed text and data attributes are escaped from external records.
        safe_model = html.escape(model, quote=True)
        mean = statistics.mean(scores)
        rows.append(
            f'<tr data-model="{safe_model}"><th scope="row">{safe_model}</th>'
            f"<td>{mean:.1f}</td><td>{len(scores)}</td></tr>"
        )
    label = "Synthetic preview — not experimental data" if synthetic else "Experiment results"
    empty = '<p class="empty">No complete experiment records are available.</p>' if not rows else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(label)}</title>"
        '<link rel="stylesheet" href="/static/styles.css"></head>'
        f'<body><main><p><a href="/">Back to experiment</a></p><h1>{html.escape(label)}</h1>{empty}'
        "<table><caption>Mean deterministic compliance score by model.</caption>"
        "<thead><tr><th>Model</th><th>Mean score</th><th>Responses</th></tr></thead>"
        f"<tbody>{''.join(rows)}"
        "</tbody></table></main></body></html>"
    )


def render_file(path) -> str:
    """Read a JSONL path and return its safely rendered standalone leaderboard."""
    # Keeping file access outside rendering makes the latter easy to test.
    return render_leaderboard(read_records(path))
