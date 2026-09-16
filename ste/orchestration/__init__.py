import time
from datetime import datetime, timezone

from ste.models.inference import call_model, judge_reply
from ste.scoring import composite, stat_score
from ste.stats import paired_t

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
    "named_rules": ("Write all replies in ASD-STE100 Simplified Technical English. " + RULES),
}

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


def run_session_generator(
    model, variant, prompts, depths, approved, session_id, api_key, judge_model=None
):
    """One conversation, yielding progress updates and records."""
    constraint = VARIANTS[variant]
    history = []
    max_depth = max(depths)

    yield {"type": "session_start", "model": model, "variant": variant, "session": session_id}

    for turn in range(1, max_depth + 1):
        history.append({"role": "user", "content": prompts[turn - 1]})

        # Start turn timing
        turn_start = time.time()

        reply, pt, ct = call_model(model, constraint, history, api_key=api_key)
        history.append({"role": "assistant", "content": reply})

        # End turn timing
        turn_time = time.time() - turn_start

        yield {
            "type": "turn_complete",
            "model": model,
            "variant": variant,
            "turn": turn,
            "turn_time": turn_time,
        }

        if turn in depths:
            st = stat_score(reply, approved)
            if st:
                overall = None
                if judge_model:
                    overall, jp, jc = judge_reply(reply, api_key=api_key, judge_model=judge_model)
                score = composite(st, overall)

                record = {
                    "session": session_id,
                    "model": model,
                    "variant": variant,
                    "depth": turn,
                    "score": score,
                    "stats": st,
                    "judge_overall": overall,
                    "reply": reply,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                yield {"type": "record", "record": record}

        time.sleep(0.4)  # SLEEP_BETWEEN_CALLS

    yield {"type": "session_complete", "model": model, "variant": variant, "session": session_id}


def estimate_batch_cost(models, depths, n_probes, sessions_per_batch, judge_model, pricing):
    """Rough USD for one batch."""
    max_depth = max(depths)
    turn_in = sum(250 + 430 * (t - 1) for t in range(1, max_depth + 1))
    turn_out = 180 * max_depth
    total = 0.0
    arms = len(VARIANTS)
    for m in models:
        i, o = pricing.get(m, (2.0, 6.0))
        per_session = turn_in * i / 1e6 + turn_out * o / 1e6
        total += per_session * sessions_per_batch * arms
    if judge_model:
        ji, jo = pricing.get(judge_model, (3.0, 15.0))
        n_judged = len(models) * sessions_per_batch * arms * n_probes
        total += n_judged * (300 * ji / 1e6 + 60 * jo / 1e6)
    return total


def contrasts(cells):
    """Returns the three orthogonal contrasts of a 2x2 design."""
    b, r = cells["bare"], cells["rules"]
    n, nr = cells["named"], cells["named_rules"]
    return {
        "naming": ((n + nr) - (b + r)) / 2.0,
        "detail": ((r + nr) - (b + n)) / 2.0,
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
        complete = [c for (s, m, d), c in obs.items() if d == depth and len(c) == len(VARIANTS)]
        if len(complete) < 2:
            continue
        out[depth] = {}
        for name in ("naming", "detail", "interaction"):
            vals = [contrasts(c)[name] for c in complete]
            out[depth][name] = paired_t(vals)
    return out


def get_records_file():
    """Returns the correct path for records depending on Cloud Run environment."""
    import os

    if os.path.exists("/experiments") and os.path.isdir("/experiments"):
        return "/experiments/records.jsonl"
    return "ste_retention_run/records.jsonl"


def get_incomplete_session(records_file, model):
    """
    Scans the records and returns (session_id, missing_variants) for the given model.
    If all sessions for the model are complete, returns (next_session_id, []).
    """
    import json
    import os

    if not os.path.exists(records_file):
        return 1, []

    obs = {}
    max_session_id = 0
    with open(records_file, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                s_id = r["session"]
                max_session_id = max(max_session_id, s_id)
                if r["model"] == model:
                    obs.setdefault(s_id, set()).add(r["variant"])
            except Exception:
                continue

    for s_id, variants in obs.items():
        if len(variants) < len(VARIANTS):
            return s_id, list(set(VARIANTS.keys()) - variants)

    return max_session_id + 1, []
