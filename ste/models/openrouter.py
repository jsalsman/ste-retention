"""Credential-safe OpenRouter HTTP communication."""

import time
from dataclasses import dataclass
from typing import Any

import requests

# A single documented upstream URL keeps callers independent from HTTP details.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# These provider statuses are the only values this text-only interaction can safely
# interpret; tool-call and filtering finishes are valid but not complete text answers.
KNOWN_FINISH_REASONS = {"stop", "length", "max_tokens", "content_filter", "tool_calls"}
TRUNCATION_FINISH_REASONS = {"length", "max_tokens"}
# Each logical completion may use two bounded follow-up calls when the provider
# reaches its output-token limit; the cap prevents unbounded cost and latency.
MAX_CONTINUATIONS = 2
# This instruction asks for only the missing suffix after replaying the partial
# assistant message, reducing repetition while preserving the original conversation.
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
    max_tokens: int = 300,
) -> ChatCompletion:
    """Return validated text, continuing token-limited responses within fixed bounds."""
    if not api_key:
        # Fail before a network call and never include credential material.
        raise UpstreamError("An OpenRouter API key is required.")
    # Copy caller-owned messages before appending partial assistant turns for continuation.
    request_messages = [dict(message) for message in messages]
    content_parts: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        for attempt in range(MAX_CONTINUATIONS + 1):
            # All provider attempts share one deadline so continuations cannot extend it.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IncompleteGenerationError("".join(content_parts))
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
            if normalized_reason not in TRUNCATION_FINISH_REASONS:
                # Join exact chunks without inventing whitespace in the model's answer.
                combined_content = "".join(content_parts)
                return ChatCompletion(
                    content=combined_content,
                    finish_reason=normalized_reason,
                    complete=normalized_reason == "stop",
                )
            if attempt < MAX_CONTINUATIONS:
                # Replay the partial assistant output before explicitly requesting its suffix.
                request_messages.extend(
                    (
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": CONTINUATION_PROMPT},
                    )
                )
        # Preserve all useful text if the fixed continuation allowance is exhausted.
        raise IncompleteGenerationError("".join(content_parts), normalized_reason)
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
    """Return validated model text for experiment runners, including safe partial text."""
    try:
        # Experiment checkpoints retain text only, while interactive calls use full metadata.
        return chat(api_key, model, messages, **options).content
    except IncompleteGenerationError as exc:
        # A token-limited answer remains useful measured work and is safe to checkpoint.
        return exc.content
