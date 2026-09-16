"""Deterministic, reusable simplified-technical-English scoring."""

import re

# These compiled patterns define the deliberately limited mechanical score.
SENTENCES = re.compile(r"(?<=[.!?])\s+")
WORDS = re.compile(r"[A-Za-z][A-Za-z'-]*")
PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|am)\b\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.I
)


def score_text(text: str, approved_words: set[str] | None = None) -> dict[str, float]:
    """Return transparent compliance metrics for untrusted model-generated text."""
    clean = re.sub(r"```.*?```", " ", text, flags=re.S)
    sentences = [part.strip() for part in SENTENCES.split(clean) if part.strip()]
    # Empty output is explicitly a zero rather than an accidental pass.
    counts = [len(WORDS.findall(sentence)) for sentence in sentences]
    length_rate = sum(count <= 25 for count in counts) / len(counts) if counts else 0.0
    active_rate = (
        sum(not PASSIVE.search(sentence) for sentence in sentences) / len(sentences)
        if sentences
        else 0.0
    )
    words = [word.lower() for word in WORDS.findall(clean)]
    vocabulary_rate = (
        sum(word in approved_words for word in words) / len(words)
        if approved_words and words
        else 0.0
    )
    # Vocabulary is omitted, not treated as failed, when no licensed list exists.
    components = [length_rate, active_rate] + ([vocabulary_rate] if approved_words else [])
    return {
        "score": round(100 * sum(components) / len(components), 2),
        "sentence_length": length_rate,
        "active_voice": active_rate,
        "approved_vocabulary": vocabulary_rate if approved_words else None,
    }
