import os
import re
import statistics

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
PASSIVE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|am)\b\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.I
)


def load_approved_words(filepath=None):
    """Loads approved words from a file, returning a set."""
    if filepath and os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as fh:
            return {w.strip().lower() for w in fh if w.strip()}
    return None


def strip_markup(text):
    """Strips common markdown formatting."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"[*_`#>|-]+", " ", text)


def stat_score(reply, approved=None):
    """Calculates statistical scores for a reply."""
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
        "pct_over_20_words": round(
            100 * sum(length_val > 20 for length_val in lengths) / len(lengths), 1
        ),
        "passive_per_sentence": round(passives / len(sents), 3),
    }
    if approved:
        bad = [w for w in words if w not in approved]
        out["pct_unapproved"] = round(100 * len(bad) / max(len(words), 1), 1)
    else:
        out["pct_unapproved"] = None
    return out


def composite(stats, judge_overall=None):
    """Calculates a composite score from stats and judge result."""
    parts = [
        100.0 - stats["pct_over_20_words"],
        100.0 * (1.0 - min(stats["passive_per_sentence"], 1.0)),
    ]
    if stats.get("pct_unapproved") is not None:
        parts.append(100.0 - stats["pct_unapproved"])
    if judge_overall is not None:
        parts.append(100.0 * float(judge_overall))
    return round(statistics.mean(parts), 2)
