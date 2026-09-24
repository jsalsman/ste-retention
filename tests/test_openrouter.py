"""Unit tests for sanitized OpenRouter completion handling."""

from unittest.mock import Mock

import pytest
import requests

from ste.models import openrouter


def _response(payload):
    """Build a successful mocked HTTP response containing the supplied JSON payload."""
    # The response passes HTTP validation before returning controlled provider JSON.
    response = Mock()
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

    assert completion == openrouter.ChatCompletion(
        content="Complete", finish_reason="stop", complete=True
    )


def test_chat_raises_distinct_error_with_partial_text_at_token_limit(monkeypatch):
    """Preserve partial output while classifying an explicit token-limit finish."""
    response = _response(
        {"choices": [{"message": {"content": "Partial"}, "finish_reason": "length"}]}
    )
    # The mock prevents credentials from reaching an external provider.
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=response))

    with pytest.raises(openrouter.IncompleteGenerationError) as caught:
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    assert caught.value.content == "Partial"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("finish_reason", [None, "", 7, "unexpected"])
def test_chat_rejects_malformed_choice_metadata(monkeypatch, finish_reason):
    """Convert absent or invalid finish metadata into one sanitized upstream error."""
    response = _response(
        {"choices": [{"message": {"content": "Text"}, "finish_reason": finish_reason}]}
    )
    # Malformed provider data is exercised locally and never invokes a paid call.
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=response))

    with pytest.raises(openrouter.UpstreamError) as caught:
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    assert str(caught.value) == "OpenRouter could not complete the request."
    assert "secret" not in str(caught.value)


def test_chat_sanitizes_provider_failure(monkeypatch):
    """Discard provider transport details, response bodies, and credential material."""
    # Simulate a transport exception whose unsafe detail must stay in the cause only.
    failure = requests.HTTPError("secret Authorization provider-body")
    monkeypatch.setattr(openrouter.requests, "post", Mock(side_effect=failure))

    with pytest.raises(openrouter.UpstreamError) as caught:
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    assert str(caught.value) == "OpenRouter could not complete the request."
    assert "secret" not in str(caught.value) and "provider-body" not in str(caught.value)
