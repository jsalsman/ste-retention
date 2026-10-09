"""Credential-safe OpenRouter HTTP communication with complete-output retries."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

# A single documented upstream URL keeps callers independent from HTTP details.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# These provider statuses are the only values this text-only interaction can safely
# interpret; tool-call and filtering finishes are valid but not complete text answers.
KNOWN_FINISH_REASONS = {"stop", "length", "max_tokens", "content_filter", "tool_calls"}
TRUNCATION_FINISH_REASONS = {"length", "max_tokens"}
# Generous output capacity reduces avoidable continuations for every interactive,
# preview, and research generation while remaining bounded for cost control.
DEFAULT_MAX_TOKENS = 8192
# A logical response gets three suffix requests after its initial provider call;
# callers receive success only after the provider explicitly reports ``stop``.
MAX_CONTINUATIONS = 3
# The suffix instruction is deliberately stable so retries can be tested and so
# the model does not mistake a continuation for a new experiment prompt.
CONTINUATION_PROMPT = "Continue exactly where the preceding response stopped. Do not repeat it."


def _reported_cost(payload: Any) -> float | None:
    """Return OpenRouter's reported US-dollar charge for one response, if usable.

    OpenRouter includes ``usage.cost`` when usage accounting is requested. A
    missing, boolean, negative, or non-finite value yields ``None`` so callers
    treat the request's charge as unknown rather than as free.
    """
    # Only a mapping can carry usage metadata; anything else has no usable cost.
    usage = payload.get("usage") if isinstance(payload, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    # Reject impossible charges so they can never reach a displayed total.
    return float(cost) if math.isfinite(cost) and cost >= 0 else None


class UpstreamError(RuntimeError):
    """Represent a sanitized inference failure safe to expose to a caller.

    ``reason`` is a short explanation built only from fixed text and, for HTTP
    failures, the numeric status code. It never contains provider response
    bodies, headers, or credentials, so it is safe to save and display.
    """

    def __init__(self, message: str, reason: str | None = None) -> None:
        """Create the error with its public message and a safe stop reason."""
        super().__init__(message)
        # Callers that give no specific reason fall back to the public message.
        self.reason = reason or message


# Plain-language meanings for the HTTP statuses OpenRouter documents. Only the
# status number comes from the response, so these reasons cannot leak its body.
HTTP_STATUS_REASONS = {
    400: "the request was rejected as invalid",
    401: "the API key was rejected",
    402: "the account or key is out of credit or has reached its spending limit",
    403: "the API key reached its own spending limit, or moderation refused the request",
    404: "the model is not available",
    408: "the request timed out",
    413: "the request was too large",
    429: "requests are being rate limited",
    500: "OpenRouter had an internal error",
    502: "the model provider returned an error",
    503: "no model provider is currently available",
}

# Reasons for replies that arrived but cannot be used, keyed by finish status.
FINISH_REASONS = {
    "length": "The model's reply was still unfinished after every allowed continuation.",
    "max_tokens": "The model's reply was still unfinished after every allowed continuation.",
    "content_filter": "The provider filtered the model's reply.",
    "tool_calls": "The model asked to call a tool instead of replying.",
}

# OpenRouter's 403 body says "Key limit exceeded" when a key reaches its own total
# spending limit. The body is only matched against this phrase, never echoed, and
# the fixed reason below tells the user exactly what to change.
KEY_LIMIT_PHRASE = "key limit exceeded"
KEY_LIMIT_REASON = (
    "OpenRouter returned HTTP 403: the API key reached its own spending limit. "
    "Raise the key's limit on OpenRouter's Keys page, then resume the run."
)

# Our own validation messages map to readable reasons; nothing else is echoed.
VALIDATION_REASONS = {
    "missing response text": "OpenRouter returned an empty reply.",
    "missing finish reason": "OpenRouter returned a reply without a finish status.",
    "unknown finish reason": "OpenRouter returned a reply with an unknown finish status.",
}


def _mentions_key_limit(response: Any) -> bool:
    """Return whether an error body contains OpenRouter's key-limit phrase.

    Only a case-insensitive substring check is made, so no body text can reach
    the stop reason. Unreadable bodies count as not mentioning the limit.
    """
    try:
        # A short prefix suffices for OpenRouter's compact JSON error object.
        text = response.text[:2000]
    except Exception:  # noqa: BLE001
        return False
    # Case-insensitive matching tolerates small wording changes in OpenRouter's reply.
    return isinstance(text, str) and KEY_LIMIT_PHRASE in text.lower()


def _failure_reason(exc: BaseException) -> str:
    """Describe a transport or validation failure using only safe, fixed text.

    Args:
        exc: The exception caught while sending the request or reading its reply.

    Returns:
        One sentence suitable for saving with a run and showing in the browser.

    """
    if isinstance(exc, requests.Timeout):
        return "OpenRouter did not respond within the time limit."
    if isinstance(exc, requests.ConnectionError):
        return "The app could not connect to OpenRouter."
    if isinstance(exc, requests.HTTPError):
        # Only the integer status is read; the response body is never touched.
        status = getattr(exc.response, "status_code", None)
        if status == 403 and _mentions_key_limit(exc.response):
            # A recognized phrase selects fixed text; the body itself is discarded.
            return KEY_LIMIT_REASON
        if isinstance(status, int):
            meaning = HTTP_STATUS_REASONS.get(status)
            return f"OpenRouter returned HTTP {status}" + (f": {meaning}." if meaning else ".")
        return "OpenRouter returned an HTTP error."
    if isinstance(exc, requests.RequestException):
        return "The request to OpenRouter failed."
    # Exception text is used only when it is one of this module's own fixed messages.
    fallback = "OpenRouter returned a response in an unexpected format."
    return VALIDATION_REASONS.get(str(exc), fallback)


@dataclass(frozen=True)
class ChatCompletion:
    """Hold validated model text and the provider's normalized completion status."""

    content: str
    finish_reason: str
    complete: bool


class IncompleteGenerationError(UpstreamError):
    """Report explicit provider truncation while retaining safe partial model text."""

    def __init__(self, content: str, finish_reason: str = "length") -> None:
        """Create a sanitized truncation result containing only model-generated text."""
        # Do not retain the response, its headers, or any credential-bearing exception.
        super().__init__(
            "OpenRouter reported that the response was truncated.",
            FINISH_REASONS.get(finish_reason, "The model's reply did not finish."),
        )
        # Both fields have passed validation and are safe for the public response.
        self.content = content
        self.finish_reason = finish_reason


def chat(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: float = 45,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    on_attempt: Callable[[], None] | None = None,
    on_cost: Callable[[float], None] | None = None,
) -> ChatCompletion:
    """Return one validated response that the provider explicitly finished.

    Token-limited output is continued with the validated partial assistant text
    in context. All attempts share ``timeout`` and use ``max_tokens`` apiece. A
    non-``stop`` result is never represented as a successful completion, and
    validated partial text is retained only for an explicit incomplete error.

    Args:
        api_key: OpenRouter credential supplied directly on each HTTP request.
        model: Exact OpenRouter model identifier selected by the caller.
        messages: Conversation messages; this function copies them before retrying.
        timeout: Maximum seconds for the complete logical response, including retries.
        max_tokens: Output-token allowance for each provider attempt.
        on_attempt: Optional durable accounting callback invoked immediately before
            each provider request. If it raises, that request is not sent.
        on_cost: Optional callback that receives each response's provider-reported
            US-dollar charge. It is not called when a response reports no usable
            cost, so callers can tell priced requests from unpriced ones.

    Returns:
        A completion whose finish reason is ``stop`` and whose content combines
        every validated continuation chunk.

    Raises:
        IncompleteGenerationError: If retries cannot reach an explicit ``stop``.
        UpstreamError: If the first request fails or provider data is malformed.

    """
    if not api_key:
        # Fail before a network call and never include credential material.
        raise UpstreamError("An OpenRouter API key is required.")
    if type(max_tokens) is not int or max_tokens <= 0 or timeout <= 0:
        # Invalid limits must fail before credentials or messages leave the process.
        raise UpstreamError("OpenRouter request limits are invalid.")
    request_messages = [dict(message) for message in messages]
    content_parts: list[str] = []
    last_reason = "length"
    # One shared deadline prevents continuation retries from multiplying latency.
    deadline = time.monotonic() + timeout
    try:
        for attempt in range(MAX_CONTINUATIONS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Partial text is diagnostic output, not completed experimental work.
                incomplete = IncompleteGenerationError("".join(content_parts), last_reason)
                # Running out of time is a different cause from a reply that never ends.
                incomplete.reason = "The model's reply did not finish within the time limit."
                raise incomplete
            if on_attempt is not None:
                # Account durably before sending so crashes cannot hide paid work.
                on_attempt()
            response = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": request_messages,
                    "max_tokens": max_tokens,
                    # Ask OpenRouter to report each response's charge for cost totals.
                    "usage": {"include": True},
                },
                timeout=remaining,
            )
            response.raise_for_status()
            payload: Any = response.json()
            cost = _reported_cost(payload)
            if on_cost is not None and cost is not None:
                # Report the charge before content checks; malformed replies still cost.
                on_cost(cost)
            # Validate the selected choice rather than returning arbitrary provider JSON.
            choice: Any = payload["choices"][0]
            content: Any = choice["message"]["content"]
            finish_reason: Any = choice["finish_reason"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("missing response text")
            # Missing, empty, or non-text metadata cannot establish a complete generation.
            if not isinstance(finish_reason, str) or not finish_reason.strip():
                raise ValueError("missing finish reason")
            normalized_reason = finish_reason.strip().lower()
            if normalized_reason not in KNOWN_FINISH_REASONS:
                raise ValueError("unknown finish reason")
            content_parts.append(content)
            if normalized_reason == "stop":
                # Only an explicit natural stop proves the combined answer is complete.
                return ChatCompletion("".join(content_parts), normalized_reason, True)
            last_reason = normalized_reason
            if normalized_reason not in TRUNCATION_FINISH_REASONS:
                # Filtering and tool calls cannot produce a complete text-only answer.
                raise IncompleteGenerationError("".join(content_parts), normalized_reason)
            if attempt < MAX_CONTINUATIONS:
                # Replay each chunk as an assistant turn and request only its missing suffix.
                request_messages.extend(
                    (
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": CONTINUATION_PROMPT},
                    )
                )
        # Exhaustion remains incomplete even though all validated chunks are preserved.
        raise IncompleteGenerationError("".join(content_parts), last_reason)
    except IncompleteGenerationError:
        # Keep truncation distinct from malformed responses and transport failures.
        raise
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        # Deliberately discard response bodies, headers, and exception strings.
        raise UpstreamError(
            "OpenRouter could not complete the request.", _failure_reason(exc)
        ) from exc


def chat_text(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    **options: Any,
) -> str:
    """Return only genuinely complete text to preview and research runners.

    The metadata wrapper is removed only after :func:`chat` observes an explicit
    provider ``stop``. Incomplete errors intentionally propagate, which prevents
    either experiment runner from scoring or checkpointing truncated output.
    Credentials remain explicit and are never placed in returned text or errors.
    """
    # Experiment checkpoints accept text only after the completion invariant holds.
    completion = chat(api_key, model, messages, **options)
    # ``chat`` guarantees this branch represents an explicit provider stop.
    return completion.content
