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


def test_chat_accounts_before_every_paid_attempt(monkeypatch):
    """Reserve each initial and continuation request before invoking transport."""
    partial = _response({"choices": [{"message": {"content": "Part"}, "finish_reason": "length"}]})
    attempts = []
    # All mocked responses truncate so every bounded attempt exercises accounting.
    post = Mock(return_value=partial)
    monkeypatch.setattr(openrouter.requests, "post", post)

    with pytest.raises(openrouter.IncompleteGenerationError):
        # The callback models a durable reservation without any paid external call.
        openrouter.chat("secret", "model", [], on_attempt=lambda: attempts.append("reserved"))

    assert len(attempts) == openrouter.MAX_CONTINUATIONS + 1
    assert post.call_count == len(attempts)


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


def test_chat_reports_each_provider_cost_and_requests_usage(monkeypatch):
    """Report every attempt's reported charge and ask OpenRouter to include it."""
    partial = _response(
        {
            "choices": [{"message": {"content": "Part"}, "finish_reason": "length"}],
            "usage": {"cost": 0.002},
        }
    )
    complete = _response(
        {
            "choices": [{"message": {"content": " end."}, "finish_reason": "stop"}],
            "usage": {"cost": 0.003},
        }
    )
    # Two local responses model one continuation; no paid request is made.
    post = Mock(side_effect=(partial, complete))
    monkeypatch.setattr(openrouter.requests, "post", post)
    costs = []

    openrouter.chat("secret", "model", [], on_cost=costs.append)

    # Each attempt's charge is reported once, and every request asks for usage.
    assert costs == [0.002, 0.003]
    assert all(call.kwargs["json"]["usage"] == {"include": True} for call in post.call_args_list)


@pytest.mark.parametrize("usage", [None, {}, {"cost": None}, {"cost": True}, {"cost": -1.0}])
def test_chat_skips_missing_or_invalid_costs(monkeypatch, usage):
    """Never report an absent or impossible charge as a real (or zero) cost."""
    payload = {"choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}]}
    if usage is not None:
        # Each case supplies one malformed usage shape alongside a valid reply.
        payload["usage"] = usage
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=_response(payload)))
    costs = []

    openrouter.chat("secret", "model", [], on_cost=costs.append)

    # The callback stays silent, so callers treat the charge as unknown.
    assert costs == []


def _http_error(status):
    """Build a requests HTTPError whose response carries a status and a secret body."""
    response = requests.Response()
    response.status_code = status
    # The body must never leak into the reason, so it holds a recognizable marker.
    response._content = b'{"error":{"message":"provider-secret-body"}}'
    return requests.HTTPError(response=response)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (_http_error(403), "HTTP 403: the API key reached its own spending limit"),
        (_http_error(429), "HTTP 429: requests are being rate limited"),
        (_http_error(418), "HTTP 418."),
        (requests.Timeout("secret"), "did not respond within the time limit"),
        (requests.ConnectionError("secret"), "could not connect to OpenRouter"),
    ],
)
def test_upstream_errors_carry_safe_specific_reasons(monkeypatch, failure, expected):
    """Explain transport failures using only fixed text and the HTTP status number."""
    response = Mock()
    # raise_for_status reproduces how requests reports a non-2xx reply.
    response.raise_for_status.side_effect = failure
    post = Mock(side_effect=failure) if not isinstance(failure, requests.HTTPError) else None
    monkeypatch.setattr(openrouter.requests, "post", post or Mock(return_value=response))

    with pytest.raises(openrouter.UpstreamError) as caught:
        openrouter.chat("secret", "model", [{"role": "user", "content": "Hi"}])

    assert expected in caught.value.reason
    # The public message is unchanged, and neither text leaks the body or key.
    assert str(caught.value) == "OpenRouter could not complete the request."
    assert "provider-secret-body" not in caught.value.reason
    assert "secret" not in caught.value.reason


def test_empty_and_filtered_replies_have_specific_reasons(monkeypatch):
    """Name empty replies and provider filtering rather than a generic failure."""
    empty = _response({"choices": [{"message": {"content": " "}, "finish_reason": "stop"}]})
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=empty))
    with pytest.raises(openrouter.UpstreamError) as caught:
        openrouter.chat("secret", "model", [])
    assert caught.value.reason == "OpenRouter returned an empty reply."

    filtered = _response(
        {"choices": [{"message": {"content": "Partial"}, "finish_reason": "content_filter"}]}
    )
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=filtered))
    with pytest.raises(openrouter.IncompleteGenerationError) as caught:
        openrouter.chat("secret", "model", [])
    assert caught.value.reason == "The provider filtered the model's reply."


def test_key_limit_403_names_the_limit_and_the_fix(monkeypatch):
    """Recognize OpenRouter's key-limit reply and say how to fix it, without echoing it."""
    failure = requests.Response()
    failure.status_code = 403
    # This is OpenRouter's real reply when a key reaches its total spending limit.
    failure._content = b'{"error":{"message":"Key limit exceeded (total limit). Manage it using https://openrouter.ai/workspaces/default/keys/abc","code":403}}'
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError(response=failure)
    monkeypatch.setattr(openrouter.requests, "post", Mock(return_value=response))

    with pytest.raises(openrouter.UpstreamError) as caught:
        openrouter.chat("secret", "model", [])

    # The reason is the fixed text, and the provider's URL never appears in it.
    assert caught.value.reason == openrouter.KEY_LIMIT_REASON
    assert "workspaces" not in caught.value.reason
