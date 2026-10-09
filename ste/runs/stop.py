"""Safe, human-readable reasons for why an experiment run stopped.

When a run stops early, the web service saves a short reason with its snapshot,
shows it in the progress stream and the run-status check, and logs it. Reasons
come only from fixed text, HTTP status numbers, and exception class names, so
they never include credentials, provider response bodies, or model output.
"""

from ste.models.openrouter import UpstreamError
from ste.research import PaidRequestCeilingError
from ste.runs.backend import LeaseUnavailableError
from ste.runs.lease import RunActiveError
from ste.runs.store import RunStoreError

# Saved when the request ends without an exception, for example when the browser
# closes the page or the hosting platform's request time limit cuts it off.
CLOSED_REASON = "The connection closed, for example by the browser or a request time limit."


def describe_stop(exc: BaseException) -> str:
    """Return a display-safe sentence explaining an exception that stopped a run.

    Args:
        exc: The exception that ended the experiment stream.

    Returns:
        A short reason built only from fixed messages, HTTP status numbers, and
        the exception's class name.

    """
    if isinstance(exc, UpstreamError):
        # Provider failures carry a reason already reduced to safe fixed text.
        return exc.reason
    if isinstance(exc, (PaidRequestCeilingError, RunActiveError, LeaseUnavailableError)):
        # These exceptions are raised only with constant messages from this codebase.
        return str(exc)
    if isinstance(exc, (OSError, RunStoreError)):
        return "Saving the run's progress failed."
    # The class name identifies the failure in logs without exposing its details.
    return f"An unexpected {type(exc).__name__} stopped the run."
