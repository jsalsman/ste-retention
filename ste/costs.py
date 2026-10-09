"""Rough cost estimates for research runs that predate provider cost tracking.

Runs recorded before the app saved OpenRouter's reported charges keep only the
text of each completed request. This module rebuilds what each request sent and
received, converts characters to tokens with a fixed ratio, and prices them with
the model catalog. The result is an estimate: it omits hidden reasoning tokens,
continuation and failed attempts, message overhead, and any price changes since
the run, so the leaderboard always labels it as estimated.
"""

from ste.models.catalog import MODELS_BY_ID
from ste.protocol import JUDGE_PROMPT_PREFIX, VARIANTS

# English text averages roughly four characters per token across common tokenizers.
# The ratio varies by model and language, which is one reason totals are estimates.
CHARS_PER_TOKEN = 4

# A judge reply is a short JSON object such as {"score": 85}; its exact text is not
# saved, so this fixed length stands in for the judge's output.
JUDGE_REPLY_CHARS = len('{"score": 100}')


def _tokens(characters: int) -> float:
    """Convert a character count to an approximate token count.

    Args:
        characters: Number of characters sent or received in one request.

    Returns:
        The character count divided by :data:`CHARS_PER_TOKEN`. Real tokenizers
        differ by model and language, so this is only an approximation.

    """
    # One fixed ratio keeps estimates comparable across models and runs.
    # Fractional tokens are fine; only the final dollar total is displayed.
    return characters / CHARS_PER_TOKEN


def _price(model: object, input_chars: int, output_chars: int) -> float | None:
    """Price one request with current catalog rates.

    Args:
        model: The OpenRouter identifier saved with the request.
        input_chars: Characters the request sent, including re-sent history.
        output_chars: Characters the model returned.

    Returns:
        The estimated US-dollar charge, or ``None`` when the model is not in the
        catalog and therefore has no price to apply.

    """
    spec = MODELS_BY_ID.get(model) if isinstance(model, str) else None
    if spec is None:
        # Retired or unknown models have no trustworthy price to apply.
        return None
    # Input and output tokens are billed at their separate per-token rates.
    return (
        _tokens(input_chars) * spec.input_usd_per_token
        + _tokens(output_chars) * spec.output_usd_per_token
    )


def estimate_run_cost(state: dict) -> float | None:
    """Estimate a research run's provider cost from its saved request text.

    Each generation's input is its variant instruction, the earlier prompts and
    replies of the same conversation, and its own prompt; its output is the saved
    reply. Each judge request sends the fixed judge instruction plus the reply it
    scores. Every request is priced at the model's current catalog rates.

    Args:
        state: A validated research snapshot with its completed ``units``.

    Returns:
        A finite, non-negative US-dollar estimate, or ``None`` when the snapshot
        lacks usable units or names a model that the catalog does not price.

    """
    units = state.get("units")
    if not isinstance(units, list) or not units:
        # Without saved requests there is nothing to base an estimate on.
        return None
    conversations: dict[tuple[object, object], list[dict]] = {}
    judges: list[dict] = []
    for unit in units:
        if not isinstance(unit, dict):
            return None
        # Generations group into conversations; judges are priced on their own.
        if unit.get("kind") == "generation":
            key = (unit.get("session_id"), unit.get("variant"))
            conversations.setdefault(key, []).append(unit)
        elif unit.get("kind") == "judge":
            judges.append(unit)
        else:
            # An unrecognized request cannot be priced, so any total would be too low.
            return None
    total = 0.0
    # Judge units save only the score, so the mean reply length stands in for theirs.
    reply_lengths: list[int] = []
    for (_session_id, variant), turns in conversations.items():
        instruction = VARIANTS.get(variant) if isinstance(variant, str) else None
        if instruction is None:
            return None
        history_chars = 0
        if any(type(unit.get("depth")) is not int for unit in turns):
            # Damaged depths cannot be ordered, so the conversation cannot be rebuilt.
            return None
        # Later turns re-send every earlier prompt and reply, so order by depth.
        for unit in sorted(turns, key=lambda item: item["depth"]):
            prompt, reply = unit.get("prompt"), unit.get("response")
            if not isinstance(prompt, str) or not isinstance(reply, str):
                return None
            input_chars = len(instruction) + history_chars + len(prompt)
            cost = _price(unit.get("model"), input_chars, len(reply))
            if cost is None:
                return None
            total += cost
            # The next turn carries this exchange forward as conversation history.
            history_chars += len(prompt) + len(reply)
            reply_lengths.append(len(reply))
    if judges:
        # Judge units do not name the reply they scored, so use the mean reply length.
        mean_reply = sum(reply_lengths) / len(reply_lengths) if reply_lengths else 0
        for unit in judges:
            cost = _price(
                unit.get("model"), len(JUDGE_PROMPT_PREFIX) + round(mean_reply), JUDGE_REPLY_CHARS
            )
            if cost is None:
                return None
            total += cost
    return total
