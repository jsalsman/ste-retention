"""Unit tests for sanitized OpenRouter completion handling."""

from unittest.mock import Mock

import pytest
import requests

from ste.models import openrouter


def _response(payload):
    """Build a successful mocked HTTP response containing the supplied JSON payload."""
    # The response passes HTTP validation before returning controlled provider JSON.
    response = Mock()
    # Both response operations behave like a successful requests response.
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_chat_returns_validated_completion_metadata(monkeypatch):
    """Return model text together with a normalized successful finish reason."""
    response = _response(
        {"choices": [{"message": {"content": "Complete"}, "finish_reason": "stop"}]}
    )
    # No paid request is made; the transport returns the local response double.
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=response))

    completion = openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # The adapter exposes only the validated text and normalized provider status.
    assert completion == openrouter.ChatCompletion(
        content="Complete", finish_reason="stop", complete=True
    )


def test_chat_continues_token_limited_output_until_explicit_stop(monkeypatch):
    """Combine bounded suffix calls and accept only an explicit natural stop."""
    partial = _response(
        {"choices": [{"message": {"content": "Partial"}, "finish_reason": "length"}]}
    )
    complete = _response(
        {"choices": [{"message": {"content": " ending."}, "finish_reason": "stop"}]}
    )
    # The mock provides one truncated chunk followed by its completed suffix.
    post = Mock(side_effect=(partial, complete))
    monkeypatch.setattr(openrouter.requests, "post", post)

    result = openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # The logical result joins exact chunks and the follow-up includes continuation context.
    assert result == openrouter.ChatCompletion("Partial ending.", "stop", True)
    assert post.call_count == 2
    follow_up = post.call_args_list[1].kwargs["json"]["messages"]
    assert follow_up[-2:] == [
        {"role": "assistant", "content": "Partial"},
        {"role": "user", "content": openrouter.CONTINUATION_PROMPT},
    ]


def test_chat_reports_incomplete_after_bounded_continuations(monkeypatch):
    """Never present exhausted continuation retries as a completed response."""
    response = _response(
        {"choices": [{"message": {"content": "Part"}, "finish_reason": "max_tokens"}]}
    )
    # Every local provider response reaches the output limit, exercising the fixed cap.
    post = Mock(return_value=response)
    monkeypatch.setattr(openrouter.requests, "post", post)

    with pytest.raises(openrouter.IncompleteGenerationError) as caught:
        # Exhaustion retains safe diagnostic text but cannot satisfy experiment runners.
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    assert caught.value.content == "Part" * (openrouter.MAX_CONTINUATIONS + 1)
    assert post.call_count == openrouter.MAX_CONTINUATIONS + 1


@pytest.mark.parametrize("finish_reason", [None, "", 7, "unexpected"])
def test_chat_rejects_malformed_choice_metadata(monkeypatch, finish_reason):
    """Convert absent or invalid finish metadata into one sanitized upstream error."""
    response = _response(
        {"choices": [{"message": {"content": "Text"}, "finish_reason": finish_reason}]}
    )
    # Malformed provider data is exercised locally and never invokes a paid call.
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=response))

    with pytest.raises(openrouter.UpstreamError) as caught:
        # Every unsupported metadata shape follows the same sanitized failure path.
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # Neither provider details nor caller credentials appear in the public exception.
    assert str(caught.value) == "OpenRouter could not complete the request."
    assert "secret" not in str(caught.value)


def test_chat_sanitizes_provider_failure(monkeypatch):
    """Discard provider transport details, response bodies, and credential material."""
    # Simulate a transport exception whose unsafe detail must stay in the cause only.
    failure = requests.HTTPError("secret Authorization provider-body")
    monkeypatch.setattr(openrouter.requests, "post", Mock(side_effect=failure))

    with pytest.raises(openrouter.UpstreamError) as caught:
        # The adapter translates requests exceptions without exposing their messages.
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # Public error text is stable and contains no unsafe transport detail.
    assert str(caught.value) == "OpenRouter could not complete the request."
    assert "secret" not in str(caught.value) and "provider-body" not in str(caught.value)


def test_chat_text_unwraps_only_complete_generations(monkeypatch):
    """Give experiment runners plain text only after a verified provider stop."""
    # A normal completion is reduced to the string expected by scoring and persistence.
    monkeypatch.setattr(
        openrouter,
        "chat",
        Mock(return_value=openrouter.ChatCompletion("Complete", "stop", True)),
    )
    assert openrouter.chat_text("secret", "model", []) == "Complete"

    # Explicitly truncated text must not be scored or persisted as completed work.
    monkeypatch.setattr(
        openrouter,
        "chat",
        Mock(side_effect=openrouter.IncompleteGenerationError("Partial", "length")),
    )
    with pytest.raises(openrouter.IncompleteGenerationError):
        openrouter.chat_text("secret", "model", [])
