"""Unit tests for display-safe run stop reasons."""

import pytest

from ste.models.openrouter import UpstreamError
from ste.research import PaidRequestCeilingError
from ste.runs.lease import ACTIVE_MESSAGE, RunActiveError
from ste.runs.stop import describe_stop
from ste.runs.store import RunStoreError


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UpstreamError("public", "OpenRouter returned HTTP 429."), "OpenRouter returned HTTP 429."),
        (PaidRequestCeilingError(), str(PaidRequestCeilingError())),
        (RunActiveError(ACTIVE_MESSAGE), ACTIVE_MESSAGE),
        (RunStoreError("secret detail"), "Saving the run's progress failed."),
        (OSError("secret path"), "Saving the run's progress failed."),
        (KeyError("secret key"), "An unexpected KeyError stopped the run."),
    ],
)
def test_describe_stop_uses_only_safe_text(exc, expected):
    """Map each failure type to fixed text, never to arbitrary exception details."""
    reason = describe_stop(exc)
    assert reason == expected
    assert "secret" not in reason
