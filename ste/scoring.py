"""Transparent approximate scoring for selected simplified-English properties."""

import json
import math
import re
import statistics
from pathlib import Path

# Patterns deliberately implement inexpensive heuristics. Sentence boundaries
# and passive constructions are linguistic approximations and have known false
# positives and negatives; they do not implement full ASD-STE100 certification.
SENTENCES = re.compile(r"(?<=[.!?])\s+")
WORDS = re.compile(r"[A-Za-z][A-Za-z'-]*")
PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|am)\b\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.I
)
MARKUP = re.compile(r"```.*?```|[*_`#>|-]+", re.S)


def load_approved_words(path: Path | None) -> frozenset[str] | None:
    """Load a runtime licensed word list, or report the component unavailable."""
    if path is None:
        # Absence is distinct from an empty list and never becomes a false pass.
        return None
    words = frozenset(
        line.strip().lower() for line in path.read_text().splitlines() if line.strip()
    )
    if not words:
        # An accidentally empty licensed file should stop paid work early.
        raise ValueError("The approved-word list is empty.")
    return words


def parse_judge_score(value: str) -> float:
    """Validate a judge response as a finite JSON score from zero through 100."""
    try:
        # Requiring one JSON object avoids accepting explanatory or injected text.
        parsed = json.loads(value)
        score = parsed["score"] if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("The judge returned malformed output.") from exc
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("The judge returned malformed output.")
    result = float(score)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError("The judge score must be finite and between 0 and 100.")
    return result


def score_text(
    text: str,
    approved_words: set[str] | frozenset[str] | None = None,
    judge_score: float | None = None,
) -> dict[str, float | int | None]:
    """Score mechanical components and average only components that are available.

    Sentence compliance uses a 25-word ceiling.  Active voice is estimated with
    a regex for a form of ``be`` followed by an ``-ed`` or ``-en`` token.  Both
    heuristics are transparent approximations rather than certification.
    """
    if not isinstance(text, str):
        raise TypeError("Scored output must be text.")
    clean = MARKUP.sub(" ", text).strip()
    sentences = [part.strip() for part in SENTENCES.split(clean) if WORDS.search(part)]
    # Markup-only and empty responses have no compliant sentences.
    lengths = [len(WORDS.findall(sentence)) for sentence in sentences]
    length_rate = sum(length <= 25 for length in lengths) / len(lengths) if lengths else 0.0
    active_rate = (
        sum(not PASSIVE.search(sentence) for sentence in sentences) / len(sentences)
        if sentences
        else 0.0
    )
    words = [word.lower() for word in WORDS.findall(clean)]
    vocabulary_rate = None
    if approved_words is not None:
        vocabulary_rate = (
            sum(word in approved_words for word in words) / len(words) if words else 0.0
        )
    if judge_score is not None:
        # Callers that already parsed a judge must still receive boundary validation.
        judge_score = parse_judge_score(json.dumps({"score": judge_score}))
    components = [100 * length_rate, 100 * active_rate]
    components.extend([] if vocabulary_rate is None else [100 * vocabulary_rate])
    components.extend([] if judge_score is None else [judge_score])
    score = round(statistics.mean(components), 2)
    return {
        "score": min(100.0, max(0.0, score)),
        "sentence_length": length_rate,
        "active_voice": active_rate,
        "approved_vocabulary": vocabulary_rate,
        "judge": judge_score,
        "n_sentences": len(sentences),
        "mean_sentence_words": round(statistics.mean(lengths), 2) if lengths else None,
        "pct_over_20_words": round(100 * sum(length_ > 20 for length_ in lengths) / len(lengths), 1)
        if lengths
        else None,
        "passive_per_sentence": round(
            sum(bool(PASSIVE.search(sentence)) for sentence in sentences) / len(sentences), 3
        )
        if sentences
        else None,
    }
