#!/usr/bin/env python3
"""
ste_retention.py

Primary question: does naming a writing standard (ASD-STE100) make a model hold
the constraint longer than describing the same rules without the name?
Secondary question: which models hold it best?

Three cost decisions drive the design.

1. PAIRED. Each session runs both constraint variants against the same model
   with the same prompts. The unit of analysis is the within-pair difference,
   which removes model-to-model and prompt-to-prompt variance and needs roughly
   half the sessions of an unpaired comparison.

2. PROBES, NOT EVERY TURN. Compliance is scored at three context depths only.
   The turns in between are still generated, because they build the context, but
   they are never sent to the judge. That cuts judge spend by about 70 percent.

3. ADAPTIVE. Sessions run in small batches. After each batch the script tests
   the pooled paired difference at the deepest probe. It stops when the result
   is clear or the budget runs out. If nothing separates at the deepest probe,
   it offers to extend the depth schedule rather than add more sessions at a
   depth where there is nothing to find.

Run:  python ste_retention.py
"""

import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------------
# GLOBALS -- edit these
# ----------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "sk-or-v1-PASTE-KEY-HERE")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Three models, not five. Each extra model multiplies cost and, under a
# multiple-comparison correction, also raises the sessions needed per model.
# Order matters only for chart colours.
MODELS = [
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-4o",
    "google/gemini-2.0-flash-001",
]

# Approximate USD per million tokens, input/output, for the cost estimate only.
# Update these when prices move; they do not affect the experiment.
PRICING = {
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "openai/gpt-4o": (2.50, 10.00),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
    "mistralai/mistral-large": (2.00, 6.00),
}

# A 2x2 design. Two factors, crossed:
#
#   NAMING  -- does the prompt identify the standard by its designation?
#   DETAIL  -- does the prompt spell the rules out, or just gesture at them?
#
# Comparing only "short named" against "long unnamed" confounds the two: any
# difference could be naming or could be prompt length. Crossing them separates
# the effects and also shows whether they interact, which is the interesting
# case. If naming only helps when the rules are absent, the name is acting as a
# retrieval key. If it helps even alongside the full rules, it is doing
# something else.

RULES = (
    "Use only common approved words. Use one instruction per sentence. "
    "Keep procedural sentences to 20 words or fewer. "
    "Keep descriptive sentences to 25 words or fewer. "
    "Use the active voice. Use the present tense. "
    "Do not use noun clusters of more than three nouns."
)

VARIANTS = {
    "bare": "Write all replies in simplified technical English.",
    "rules": "Write all replies in simplified technical English. " + RULES,
    "named": "Write all replies in ASD-STE100 Simplified Technical English.",
    "named_rules": (
        "Write all replies in ASD-STE100 Simplified Technical English. " + RULES
    ),
}

# (is_named, has_rules) for each variant, used to build the contrasts.
FACTORS = {
    "bare": (0, 0),
    "rules": (0, 1),
    "named": (1, 0),
    "named_rules": (1, 1),
}

# Context depths at which compliance is scored, in turns. The first is the
# baseline. Everything between probes is filler that builds context.
DEPTH_SCHEDULE = [1, 6, 12]

# If the deepest probe shows no separation, the script offers these instead.
DEPTH_EXTENSIONS = [[1, 10, 20], [1, 16, 32]]

SESSIONS_PER_BATCH = 6      # per model, per batch
MAX_BATCHES = 6
BUDGET_USD = 40.0

# Sequential testing inflates false positives. Checking after each of six
# batches costs roughly this much alpha under a Pocock-style constant boundary.
NOMINAL_ALPHA = 0.05
SEQUENTIAL_ALPHA = 0.0158    # Pocock boundary, 6 looks, two-sided 0.05

OUTPUT_DIR = "ste_retention_run"
RECORDS_FILE = os.path.join(OUTPUT_DIR, "records.jsonl")

JUDGE_MODEL = "anthropic/claude-sonnet-4.5"
USE_JUDGE = True             # False scores on the deterministic metrics alone

TEMPERATURE = 0.7            # session variance is the point; do not use 0
MAX_TOKENS = 600
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_BACKOFF = 4
SLEEP_BETWEEN_CALLS = 0.4

# The real ASD-STE100 dictionary is copyrighted and not shipped here. Point this
# at a newline-separated word list obtained under the standard's own terms, or
# leave it None and the vocabulary term drops out of the score.
APPROVED_WORDS_FILE = None

TASK_PROMPTS = [
    "Explain how a centrifugal pump moves fluid.",
    "Describe the steps to replace a cabin air filter.",
    "Explain what causes cavitation in a hydraulic system.",
    "Describe how to inspect a drive belt for wear.",
    "Explain the purpose of a torque wrench and how to use one.",
    "Describe the steps to bleed air from a brake line.",
    "Explain how a thermostat regulates coolant flow.",
    "Describe how to test a battery with a multimeter.",
    "Explain why fasteners are tightened in a cross pattern.",
    "Describe the steps to clean a fuel injector.",
    "Explain how a check valve prevents backflow.",
    "Describe how to measure runout on a rotating shaft.",
    "Explain the function of a pressure relief valve.",
    "Describe the steps to align a coupling between two shafts.",
    "Explain how corrosion forms on dissimilar metals in contact.",
    "Describe how to inspect a weld for surface defects.",
    "Explain how a differential splits torque between two wheels.",
    "Describe the steps to purge a refrigerant line.",
    "Explain what a duty cycle means for a compressor.",
    "Describe how to set the gap on a spark plug.",
    "Explain how a strain gauge measures deformation.",
    "Describe the steps to replace a mechanical seal.",
    "Explain how a heat exchanger transfers energy.",
    "Describe how to verify the calibration of a pressure gauge.",
    "Explain why hydraulic fluid must be kept free of air.",
    "Describe the steps to remove a bearing from a shaft.",
    "Explain how a solenoid valve opens and closes.",
    "Describe how to inspect a hose for internal damage.",
    "Explain what causes a pump to lose prime.",
    "Describe the steps to torque a flange joint.",
    "Explain how a filter bypass valve protects a system.",
    "Describe how to measure the thickness of a brake disc.",
]

# ----------------------------------------------------------------------------
# STATISTICS (no scipy; keeps the dependency list at requests alone)
# ----------------------------------------------------------------------------


def _betacf(a, b, x, itmax=200, eps=3e-7):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


def paired_t(diffs):
    """-> (t, df, two-tailed p, mean, Cohen's d_z, 95% CI half-width)"""
    n = len(diffs)
    if n < 2:
        return None
    mean = statistics.mean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return (float("inf") if mean else 0.0, n - 1, 0.0 if mean else 1.0, mean, 0.0, 0.0)
    se = sd / math.sqrt(n)
    t = mean / se
    df = n - 1
    p = _betai(df / 2.0, 0.5, df / (df + t * t))
    return (t, df, p, mean, mean / sd, 1.96 * se)


# ----------------------------------------------------------------------------
# SCORING
# ----------------------------------------------------------------------------

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
PASSIVE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|am)\b\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.I
)


def load_approved_words():
    if APPROVED_WORDS_FILE and os.path.exists(APPROVED_WORDS_FILE):
        with open(APPROVED_WORDS_FILE, encoding="utf-8") as fh:
            return {w.strip().lower() for w in fh if w.strip()}
    return None


def strip_markup(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"[*_`#>|-]+", " ", text)


def stat_score(reply, approved=None):
    body = strip_markup(reply)
    sents = [s.strip() for s in SENTENCE_SPLIT.split(body) if len(s.strip()) > 1]
    if not sents:
        return None
    lengths = [len(WORD_RE.findall(s)) for s in sents]
    words = [w.lower() for w in WORD_RE.findall(body)]
    passives = len(PASSIVE_RE.findall(body))

    out = {
        "n_sentences": len(sents),
        "mean_sentence_words": round(statistics.mean(lengths), 2),
        "pct_over_20_words": round(100 * sum(l > 20 for l in lengths) / len(lengths), 1),
        "passive_per_sentence": round(passives / len(sents), 3),
    }
    if approved:
        bad = [w for w in words if w not in approved]
        out["pct_unapproved"] = round(100 * len(bad) / max(len(words), 1), 1)
    else:
        out["pct_unapproved"] = None
    return out


def composite(stats, judge_overall=None):
    parts = [
        100.0 - stats["pct_over_20_words"],
        100.0 * (1.0 - min(stats["passive_per_sentence"], 1.0)),
    ]
    if stats.get("pct_unapproved") is not None:
        parts.append(100.0 - stats["pct_unapproved"])
    if judge_overall is not None:
        parts.append(100.0 * float(judge_overall))
    return round(statistics.mean(parts), 2)


JUDGE_SYSTEM = (
    "You score technical writing against ASD-STE100 Simplified Technical English. "
    "You see one passage at a time with no information about its origin. "
    "Return ONLY a JSON object, no prose and no code fences, with keys "
    "approved_vocabulary, one_idea_per_sentence, sentence_length, active_voice, "
    "present_tense, noun_cluster_limit, overall, each scored 0.0 to 1.0."
)

# ----------------------------------------------------------------------------
# GENERATION
# ----------------------------------------------------------------------------


def call_model(model, system_text, messages, max_tokens=None):
    payload = {
        "model": model,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens or MAX_TOKENS,
        "messages": [{"role": "system", "content": system_text}] + messages,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=payload,
                              timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage", {})
            return (
                data["choices"][0]["message"]["content"],
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"{model} failed: {last}")


def judge_reply(reply):
    raw, pt, ct = call_model(
        JUDGE_MODEL, JUDGE_SYSTEM,
        [{"role": "user", "content": f"Passage:\n\n{reply}"}], max_tokens=200,
    )
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned).get("overall"), pt, ct
    except json.JSONDecodeError:
        return None, pt, ct


def run_session(model, variant, prompts, depths, approved, session_id, spend):
    """One conversation. Returns {depth: composite score}."""
    constraint = VARIANTS[variant]
    history, scores = [], {}
    max_depth = max(depths)
    in_p, out_p = PRICING.get(model, (2.0, 6.0))

    for turn in range(1, max_depth + 1):
        history.append({"role": "user", "content": prompts[turn - 1]})
        reply, pt, ct = call_model(model, constraint, history)
        history.append({"role": "assistant", "content": reply})
        spend[0] += pt * in_p / 1e6 + ct * out_p / 1e6

        if turn in depths:
            st = stat_score(reply, approved)
            if st:
                overall = None
                if USE_JUDGE:
                    ji, jp, jc = PRICING.get(JUDGE_MODEL, (3.0, 15.0)), 0, 0
                    overall, jp, jc = judge_reply(reply)
                    spend[0] += jp * ji[0] / 1e6 + jc * ji[1] / 1e6
                scores[turn] = composite(st, overall)

                with open(RECORDS_FILE, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "session": session_id,
                        "model": model,
                        "variant": variant,
                        "depth": turn,
                        "score": scores[turn],
                        "stats": st,
                        "judge_overall": overall,
                        "reply": reply,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, ensure_ascii=False) + "\n")
        time.sleep(SLEEP_BETWEEN_CALLS)

    return scores


# ----------------------------------------------------------------------------
# COST ESTIMATE
# ----------------------------------------------------------------------------


def estimate_batch_cost(depths, n_probes):
    """Rough USD for one batch: every model, SESSIONS_PER_BATCH pairs."""
    max_depth = max(depths)
    # Input grows quadratically with turns; ~250 in and ~180 out per turn.
    turn_in = sum(250 + 430 * (t - 1) for t in range(1, max_depth + 1))
    turn_out = 180 * max_depth
    total = 0.0
    arms = len(VARIANTS)
    for m in MODELS:
        i, o = PRICING.get(m, (2.0, 6.0))
        per_session = turn_in * i / 1e6 + turn_out * o / 1e6
        total += per_session * SESSIONS_PER_BATCH * arms
    if USE_JUDGE:
        ji, jo = PRICING.get(JUDGE_MODEL, (3.0, 15.0))
        n_judged = len(MODELS) * SESSIONS_PER_BATCH * arms * n_probes
        total += n_judged * (300 * ji / 1e6 + 60 * jo / 1e6)
    return total


# ----------------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------------


def contrasts(cells):
    """
    cells: {variant: score} for one session at one depth, all four present.

    Returns the three orthogonal contrasts of a 2x2 design. Each is a single
    number per session, so each gets a one-sample t-test across sessions, and
    the pairing is exact because all four came from the same prompt sequence.
    """
    b, r = cells["bare"], cells["rules"]
    n, nr = cells["named"], cells["named_rules"]
    return {
        # Does naming help, averaged over whether the rules are present?
        "naming": ((n + nr) - (b + r)) / 2.0,
        # Does spelling the rules out help, averaged over naming?
        "detail": ((r + nr) - (b + n)) / 2.0,
        # Does naming help less when the rules are already there? A negative
        # value means the two are partly redundant, which is what you expect
        # if the name is mainly a shortcut to the same content.
        "interaction": (nr - n) - (r - b),
    }


def analyse(obs):
    """
    obs: {(session, model, depth): {variant: score}}
    -> {depth: {contrast: paired_t result}}
    """
    out = {}
    depths = sorted({d for _, _, d in obs})
    for depth in depths:
        complete = [
            c for (s, m, d), c in obs.items()
            if d == depth and len(c) == len(VARIANTS)
        ]
        if len(complete) < 2:
            continue
        out[depth] = {}
        for name in ("naming", "detail", "interaction"):
            vals = [contrasts(c)[name] for c in complete]
            out[depth][name] = paired_t(vals)
    return out


def main():
    if "PASTE-KEY-HERE" in OPENROUTER_API_KEY:
        print("Set OPENROUTER_API_KEY first.")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    approved = load_approved_words()
    if approved is None:
        print("No approved word list; vocabulary term omitted from the score.\n")

    depths = list(DEPTH_SCHEDULE)
    extensions = list(DEPTH_EXTENSIONS)
    spend = [0.0]
    obs = {}
    session_id = 0

    est = estimate_batch_cost(depths, len(depths))
    print(f"Depths {depths}. {len(VARIANTS)} arms: {', '.join(VARIANTS)}.")
    print(f"Estimated {est:.2f} USD per batch, {SESSIONS_PER_BATCH} sessions "
          f"per model.")
    print(f"Budget {BUDGET_USD:.2f} USD, up to {MAX_BATCHES} batches.")
    if input("Proceed? [y/N] ").strip().lower() != "y":
        return

    for batch in range(1, MAX_BATCHES + 1):
        print(f"\n--- batch {batch} (spent {spend[0]:.2f}) ---")
        for model in MODELS:
            for _ in range(SESSIONS_PER_BATCH):
                session_id += 1
                # One prompt sequence, reused by all four arms. That is what
                # makes every contrast a within-session paired quantity.
                rng = random.Random(session_id)
                prompts = rng.sample(TASK_PROMPTS, max(depths))
                for variant in VARIANTS:
                    try:
                        got = run_session(model, variant, prompts, depths,
                                          approved, session_id, spend)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {model} s{session_id} {variant}: {exc}")
                        continue
                    for d, score in got.items():
                        obs.setdefault((session_id, model, d), {})[variant] = score
            done = sum(1 for c in obs.values() if len(c) == len(VARIANTS))
            print(f"  {model}: {done} complete cells so far")

        results = analyse(obs)
        deep = max(depths)
        print(f"\n  spent {spend[0]:.2f} USD")
        for d in sorted(results):
            print(f"  depth {d}:")
            for name in ("naming", "detail", "interaction"):
                res = results[d].get(name)
                if not res:
                    continue
                _, _, p, mean, dz, ci = res
                print(f"    {name:<12} {mean:+6.1f} +/-{ci:4.1f}  "
                      f"d_z={dz:+.2f}  p={p:.4f}")

        primary = results.get(deep, {}).get("naming")
        if primary and primary[2] < SEQUENTIAL_ALPHA:
            print(f"\nStop: naming effect at depth {deep} has p={primary[2]:.4f}, "
                  f"inside the {SEQUENTIAL_ALPHA} sequential boundary.")
            break

        if spend[0] > BUDGET_USD:
            print("\nStop: budget reached.")
            break

        # Adaptive depth: if the naming effect is flat at the deepest probe,
        # more context is a better bet than more sessions at a depth where
        # there is nothing to find.
        if primary and abs(primary[4]) < 0.2 and extensions:
            depths = extensions.pop(0)
            print(f"\nNo naming effect at depth {deep}. Extending to {depths}.")
            print(f"Next batch costs about "
                  f"{estimate_batch_cost(depths, len(depths)):.2f} USD.")

    complete = sum(1 for c in obs.values() if len(c) == len(VARIANTS))
    print(f"\nTotal spend {spend[0]:.2f} USD, {complete} complete cells.")
    print(f"Records in {RECORDS_FILE}. Run make_leaderboard.py next.")


if __name__ == "__main__":
    main()
