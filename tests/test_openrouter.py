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


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
def test_chat_continues_token_limited_responses(monkeypatch, finish_reason):
    """Request and combine a missing suffix after either token-limit finish reason."""
    partial = _response(
        {"choices": [{"message": {"content": "Partial "}, "finish_reason": finish_reason}]}
    )
    complete = _response({"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]})
    # Sequential local responses represent the initial and continuation provider calls.
    post = Mock(side_effect=(partial, complete))
    monkeypatch.setattr(openrouter.requests, "post", post)

    completion = openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # Exact chunks are combined, and the follow-up includes the partial assistant turn.
    assert completion == openrouter.ChatCompletion("Partial answer", "stop", True)
    continuation_messages = post.call_args_list[1].kwargs["json"]["messages"]
    assert continuation_messages[-2:] == [
        {"role": "assistant", "content": "Partial "},
        {"role": "user", "content": openrouter.CONTINUATION_PROMPT},
    ]


def test_chat_reports_combined_partial_text_after_continuation_limit(monkeypatch):
    """Stop retrying after the bounded allowance and retain every generated chunk."""
    # Three truncated responses consume the initial request and two allowed continuations.
    responses = [
        _response({"choices": [{"message": {"content": part}, "finish_reason": "length"}]})
        for part in ("One ", "two ", "three")
    ]
    post = Mock(side_effect=responses)
    monkeypatch.setattr(openrouter.requests, "post", post)

    with pytest.raises(openrouter.IncompleteGenerationError) as caught:
        # Exhaustion remains distinct from malformed metadata or transport failures.
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # The retry cap bounds paid calls while exposing only safe combined model text.
    assert post.call_count == openrouter.MAX_CONTINUATIONS + 1
    assert caught.value.content == "One two three"
    assert "secret" not in str(caught.value)


def test_chat_reports_expiry_before_first_request_as_upstream_error(monkeypatch):
    """Reject an expired initial deadline rather than returning an empty partial result."""
    # A zero timeout deterministically expires before any provider request can begin.
    post = Mock()
    monkeypatch.setattr(openrouter.requests, "post", post)

    with pytest.raises(openrouter.UpstreamError) as caught:
        # No generated chunk exists, so the experiment runner must see a failed unit.
        openrouter.chat("secret", "model", [], timeout=0)

    # The failure is sanitized, and no paid inference was attempted or checkpointed.
    assert str(caught.value) == "OpenRouter could not complete the request."
    post.assert_not_called()


@pytest.mark.parametrize(
    "continuation_failure",
    [
        requests.Timeout("secret transport detail"),
        _response({"choices": [{"message": {"content": "bad"}, "finish_reason": None}]}),
    ],
)
def test_chat_preserves_partial_text_when_continuation_fails(monkeypatch, continuation_failure):
    """Retain validated initial text when a continuation transport or payload fails."""
    # The initial paid call yields useful text and explicitly requests continuation.
    partial = _response(
        {"choices": [{"message": {"content": "Useful partial"}, "finish_reason": "length"}]}
    )
    # Either an exception or malformed response exercises the sanitized recovery path.
    post = Mock(side_effect=(partial, continuation_failure))
    monkeypatch.setattr(openrouter.requests, "post", post)

    with pytest.raises(openrouter.IncompleteGenerationError) as caught:
        # Continuation failure must remain distinguishable from a wholly failed inference.
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    # Only validated content survives; provider details and credentials remain excluded.
    assert caught.value.content == "Useful partial"
    assert caught.value.finish_reason == "length"
    assert caught.value.__cause__ is None
    assert post.call_count == 2


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


def test_chat_text_unwraps_complete_and_incomplete_generations(monkeypatch):
    """Give experiment runners plain text for complete and token-limited responses."""
    # A normal completion is reduced to the string expected by scoring and persistence.
    monkeypatch.setattr(
        openrouter,
        "chat",
        Mock(return_value=openrouter.ChatCompletion("Complete", "stop", True)),
    )
    assert openrouter.chat_text("secret", "model", []) == "Complete"

    # Explicitly truncated text remains useful, safe measured work for resumable studies.
    monkeypatch.setattr(
        openrouter,
        "chat",
        Mock(side_effect=openrouter.IncompleteGenerationError("Partial", "length")),
    )
    assert openrouter.chat_text("secret", "model", []) == "Partial"
