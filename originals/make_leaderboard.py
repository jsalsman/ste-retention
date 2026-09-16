#!/usr/bin/env python3
"""
make_leaderboard.py

Reads ste_retention_run/records.jsonl and writes one self-contained HTML page.

The page leads with the two main effects of the 2x2 design: does naming the
standard help, and does spelling the rules out help, each averaged over the
other. The four cell means and the per-model breakdown follow.

    python make_leaderboard.py
"""

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ste_retention import paired_t, contrasts, VARIANTS  # noqa: E402

RECORDS_FILE = "ste_retention_run/records.jsonl"
OUTPUT_HTML = "leaderboard.html"

PAGE_TITLE = "Does naming a standard make a model follow it?"
SUBTITLE = (
    "ASD-STE100 Simplified Technical English. Four prompt variants cross two "
    "factors: whether the standard is named, and whether its rules are spelled out."
)
REPO_URL = "https://github.com/yourname/ste-retention"

POS = "#1F5673"
NEG = "#9B3324"

LABELS = {
    "bare": "Neither",
    "named": "Named only",
    "rules": "Rules only",
    "named_rules": "Named and rules",
}
EFFECT_LABELS = {
    "naming": "Naming the standard",
    "detail": "Spelling out the rules",
    "interaction": "Interaction",
}
EFFECT_NOTES = {
    "naming": "averaged over whether the rules were present",
    "detail": "averaged over whether the standard was named",
    "interaction": "negative means the two overlap",
}


def load():
    if not os.path.exists(RECORDS_FILE):
        print(f"No records at {RECORDS_FILE}.")
        sys.exit(1)
    obs = {}
    with open(RECORDS_FILE, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["session"], r["model"], r["depth"])
            obs.setdefault(key, {})[r["variant"]] = r["score"]
    return {k: v for k, v in obs.items() if len(v) == len(VARIANTS)}


def summarise(obs):
    depths = sorted({d for _, _, d in obs})
    models = []
    for _, m, _ in obs:
        if m not in models:
            models.append(m)

    effects, cells, per_model = {}, {}, {}
    for d in depths:
        rows = [c for (s, m, dd), c in obs.items() if dd == d]
        if len(rows) < 2:
            continue
        effects[d] = {
            name: paired_t([contrasts(c)[name] for c in rows])
            for name in ("naming", "detail", "interaction")
        }
        cells[d] = {
            v: statistics.mean(c[v] for c in rows) for v in VARIANTS
        }

    for m in models:
        per_model[m] = {}
        for d in depths:
            rows = [c for (s, mm, dd), c in obs.items() if mm == m and dd == d]
            if len(rows) >= 2:
                per_model[m][d] = {
                    "n": len(rows),
                    "naming": paired_t([contrasts(c)["naming"] for c in rows]),
                    "cells": {v: statistics.mean(c[v] for c in rows)
                              for v in VARIANTS},
                }
    return depths, models, effects, cells, per_model


def build_effects_chart(depths, effects):
    """Horizontal effect plot: each row is one contrast at one depth, with CI."""
    rows = []
    for d in depths:
        rows.append(("head", f"Turn {d}", None))
        for name in ("naming", "detail", "interaction"):
            res = effects.get(d, {}).get(name)
            if res:
                rows.append(("eff", name, res))

    span = max(
        [abs(r[2][3]) + r[2][5] for r in rows if r[0] == "eff"] or [10]
    )
    span = max(span * 1.15, 5)

    row_h = 30
    W, L, R, T = 780, 210, 60, 26
    H = T + len(rows) * row_h + 42
    pw = W - L - R

    def px(v):
        return L + (v + span) / (2 * span) * pw

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
           f'aria-label="Effect of each factor on compliance, with confidence intervals">']

    step = 5 if span <= 20 else 10
    tick = -int(span // step) * step
    while tick <= span:
        out.append(f'<line x1="{px(tick):.0f}" y1="{T-8}" x2="{px(tick):.0f}" '
                   f'y2="{H-36}" class="grid"/>')
        out.append(f'<text x="{px(tick):.0f}" y="{H-18}" class="axis ac">'
                   f'{tick:+d}</text>')
        tick += step
    out.append(f'<line x1="{px(0):.0f}" y1="{T-8}" x2="{px(0):.0f}" '
               f'y2="{H-36}" class="zero"/>')

    y = T
    for kind, label, res in rows:
        if kind == "head":
            out.append(f'<text x="12" y="{y+18}" class="depthlab">{label}</text>')
            y += row_h
            continue
        _, _, p, mean, dz, ci = res
        colour = POS if mean >= 0 else NEG
        faint = "" if p < 0.05 else ' opacity="0.42"'
        cy = y + 13
        out.append(f'<text x="{L-14}" y="{cy+4}" class="efflab ar">'
                   f'{EFFECT_LABELS[label]}</text>')
        out.append(f'<line x1="{px(mean-ci):.1f}" y1="{cy}" '
                   f'x2="{px(mean+ci):.1f}" y2="{cy}" stroke="{colour}" '
                   f'stroke-width="2"{faint}/>')
        for end in (mean - ci, mean + ci):
            out.append(f'<line x1="{px(end):.1f}" y1="{cy-4}" x2="{px(end):.1f}" '
                       f'y2="{cy+4}" stroke="{colour}" stroke-width="2"{faint}/>')
        out.append(f'<circle cx="{px(mean):.1f}" cy="{cy}" r="5" '
                   f'fill="{colour}"{faint}/>')
        star = " *" if p < 0.05 else ""
        out.append(f'<text x="{W-R+8}" y="{cy+4}" class="delta">'
                   f'{mean:+.1f}{star}</text>')
        y += row_h

    out.append("</svg>")
    return "\n".join(out)


def build_cells_table(depths, cells):
    heads = "".join(f"<th>Turn {d}</th>" for d in depths)
    rows = []
    for v in ("bare", "named", "rules", "named_rules"):
        tds = "".join(
            f"<td>{cells[d][v]:.1f}</td>" if d in cells else "<td>&mdash;</td>"
            for d in depths
        )
        rows.append(f"<tr><td class='lead'>{LABELS[v]}</td>{tds}</tr>")
    return f"""
<table>
  <caption>Mean compliance score for each of the four prompt variants. These are
  the raw cells the effects above are computed from.</caption>
  <thead><tr><th class='lead'>Prompt contains</th>{heads}</tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def build_effects_table(depths, effects):
    rows = []
    for d in depths:
        for name in ("naming", "detail", "interaction"):
            res = effects.get(d, {}).get(name)
            if not res:
                continue
            _, df, p, mean, dz, ci = res
            cls = "clear" if p < 0.05 else ("weak" if p < 0.15 else "none")
            rows.append(
                f"<tr><td class='lead'>Turn {d}</td>"
                f"<td class='lead'>{EFFECT_LABELS[name]}"
                f"<span class='hint'>{EFFECT_NOTES[name]}</span></td>"
                f"<td>{mean:+.1f}</td><td>&plusmn;{ci:.1f}</td>"
                f"<td>{dz:+.2f}</td><td>{df+1}</td>"
                f"<td class='{cls}'>{p:.4f}</td></tr>"
            )
    return f"""
<table>
  <caption>All three contrasts are within-session: every variant answered the
  same questions in the same order, so prompt and model variance cancel.</caption>
  <thead><tr><th class='lead'>Depth</th><th class='lead'>Factor</th>
  <th>Effect</th><th>95% CI</th><th>d<sub>z</sub></th><th>Sessions</th>
  <th>p</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def build_model_table(depths, models, per_model):
    heads = "".join(f"<th>Turn {d}</th>" for d in depths)
    rows = []
    for m in models:
        cells = []
        for d in depths:
            c = per_model.get(m, {}).get(d)
            if not c or not c["naming"]:
                cells.append("<td>&mdash;</td>")
                continue
            _, _, p, mean, _, _ = c["naming"]
            star = "*" if p < 0.05 else ""
            cells.append(f"<td>{mean:+.1f}{star}</td>")
        rows.append(f"<tr><td class='model'>{m}</td>{''.join(cells)}</tr>")
    return f"""
<table>
  <caption>Naming effect for each model on its own. Secondary result: each rests
  on a third of the sessions behind the pooled figures, and no correction for
  multiple comparisons is applied.</caption>
  <thead><tr><th class='model'>Model</th>{heads}</tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


CSS = """
:root{--paper:#E9EDF1;--panel:#fff;--ink:#16222E;--muted:#5C6B78;--rule:#C6D0D9;
--pos:#1F5673;--neg:#9B3324;--in:#21665A;--caution:#9C6B08}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
font-size:16px;line-height:1.55;font-variant-numeric:tabular-nums}
.wrap{max-width:880px;margin:0 auto;padding:56px 24px 80px}
h1{font-size:2rem;font-weight:600;letter-spacing:-.02em;margin:0 0 .35em;max-width:20ch}
.sub{color:var(--muted);margin:0 0 1em;max-width:62ch}
.verdict{margin:0 0 2.2em;max-width:62ch}
.board{background:var(--panel);border:1px solid var(--rule);padding:26px;margin-bottom:32px}
.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--rule);stroke-width:1}
.zero{stroke:var(--ink);stroke-width:1.5;opacity:.55}
.axis{fill:var(--muted);font-size:11px}
.ac{text-anchor:middle}
.ar{text-anchor:end}
.efflab{fill:var(--ink);font-size:12.5px}
.depthlab{fill:var(--muted);font-size:12.5px;font-weight:500}
.delta{fill:var(--ink);font-size:12px;font-weight:600;text-anchor:start}
.legend{margin-top:18px;padding-top:16px;border-top:1px solid var(--rule);
font-size:.85rem;color:var(--muted);max-width:64ch}
h2{font-size:1.1rem;font-weight:600;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:.88rem}
caption{text-align:left;color:var(--muted);font-size:.84rem;padding-bottom:12px;max-width:64ch}
th,td{padding:8px 6px;text-align:right;border-bottom:1px solid var(--rule)}
th{font-weight:500;color:var(--muted);font-size:.8rem}
.lead,.model{text-align:left}
.model{font-family:"IBM Plex Mono",monospace;font-size:.78rem}
.hint{display:block;color:var(--muted);font-size:.76rem;line-height:1.3}
.clear{color:var(--in);font-weight:600}
.weak{color:var(--caution)}
.none{color:var(--muted)}
footer{margin-top:44px;padding-top:22px;border-top:1px solid var(--rule);
color:var(--muted);font-size:.85rem;max-width:64ch}
a{color:var(--ink)}
a:focus-visible{outline:2px solid var(--pos);outline-offset:3px}
@media(max-width:620px){.wrap{padding:34px 14px 60px}h1{font-size:1.5rem}
table{font-size:.78rem}th,td{padding:6px 3px}}
"""


def main():
    obs = load()
    if not obs:
        print("No complete 2x2 cells found yet.")
        sys.exit(1)

    depths, models, effects, cells, per_model = summarise(obs)
    deep = max(depths)

    nm = effects.get(deep, {}).get("naming")
    dt = effects.get(deep, {}).get("detail")
    if nm and dt:
        n_sig = "held up" if nm[2] < 0.05 else "did not reach significance"
        verdict = (
            f"At turn {deep}, naming the standard moved compliance by "
            f"{nm[3]:+.1f} points (95% CI &plusmn;{nm[5]:.1f}, p={nm[2]:.4f}) and "
            f"{n_sig}. Spelling out the rules moved it {dt[3]:+.1f} points "
            f"(p={dt[2]:.4f}). Because the factors are crossed, neither figure "
            f"is explained by the other prompt simply being longer."
        )
    else:
        verdict = "Not enough complete sessions yet for a test at the deepest probe."

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{PAGE_TITLE}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="wrap">
  <h1>{PAGE_TITLE}</h1>
  <p class="sub">{SUBTITLE}</p>
  <p class="verdict">{verdict}</p>
  <div class="board">
    {build_effects_chart(depths, effects)}
    <p class="legend">Points are the mean effect in compliance points, bars are
    95% confidence intervals, and faded rows do not clear p&lt;0.05. A bar that
    crosses zero means the factor made no reliable difference at that depth.</p>
  </div>
  <div class="board"><h2>Primary result</h2>{build_effects_table(depths, effects)}</div>
  <div class="board"><h2>The four cells</h2>{build_cells_table(depths, cells)}</div>
  <div class="board"><h2>By model</h2>{build_model_table(depths, models, per_model)}</div>
  <footer>
    Each session runs all four prompt variants against one model over the same
    question sequence, and compliance is scored at three context depths. Scores
    combine sentence length, active voice, approved vocabulary, and a judge
    model that never sees which variant produced a passage. Testing stopped at a
    Pocock sequential boundary, not at the first significant look. Raw replies
    and code are at <a href="{REPO_URL}">{REPO_URL}</a>.
  </footer>
</div></body></html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {OUTPUT_HTML}: {len(obs)} complete cells, "
          f"{len(models)} models, depths {depths}")


if __name__ == "__main__":
    main()
