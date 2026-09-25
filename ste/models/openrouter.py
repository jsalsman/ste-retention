"""Credential-safe OpenRouter HTTP communication with complete-output retries."""

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


class UpstreamError(RuntimeError):
    """Represent a sanitized inference failure safe to expose to a caller."""


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
        super().__init__("OpenRouter reported that the response was truncated.")
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
                raise IncompleteGenerationError("".join(content_parts), last_reason)
            if on_attempt is not None:
                # Account durably before sending so crashes cannot hide paid work.
                on_attempt()
            response = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "messages": request_messages, "max_tokens": max_tokens},
                timeout=remaining,
            )
            response.raise_for_status()
            # Validate the selected choice rather than returning arbitrary provider JSON.
            choice: Any = response.json()["choices"][0]
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
        raise UpstreamError("OpenRouter could not complete the request.") from exc


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
