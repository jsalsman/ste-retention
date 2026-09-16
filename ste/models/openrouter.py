"""Credential-safe OpenRouter HTTP communication."""

from typing import Any

import requests

# A single documented upstream URL keeps callers independent from HTTP details.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class UpstreamError(RuntimeError):
    """Represent a sanitized inference failure safe to expose to a caller."""


def chat(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: float = 45,
    max_tokens: int = 300,
) -> str:
    """Send one chat request using an explicitly supplied, never-returned API key."""
    if not api_key:
        # Fail before a network call and never include credential material.
        raise UpstreamError("An OpenRouter API key is required.")
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
            timeout=timeout,
        )
        response.raise_for_status()
        # Validate the narrow response shape instead of returning arbitrary JSON.
        content: Any = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("missing response text")
        return content
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        # Deliberately discard response bodies, headers, and exception strings.
        raise UpstreamError("OpenRouter could not complete the request.") from exc
