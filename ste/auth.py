"""Small deployment gate for authenticated, owner-scoped paid operations."""

import hmac
import json
import os
import re
import time
from collections import defaultdict, deque

from flask import Request

# Authentication is intentionally an adapter boundary. Production deployments
# should set AUTH_REQUIRED=true and inject a secret-to-user mapping, or replace
# this module with verified Cloud Run IAM identity headers at a trusted proxy.
USER_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_REQUESTS: dict[str, deque[float]] = defaultdict(deque)


class AuthError(PermissionError):
    """Represent a stable authentication or authorization failure."""


def authenticate(request: Request) -> str:
    """Return a verified user ID, failing closed when production auth is enabled."""
    required = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"
    if not required:
        # Local-only mode is explicit in documentation and never suitable publicly.
        return "local"
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    try:
        configured = json.loads(os.environ.get("AUTH_TOKENS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise AuthError("Authentication is unavailable.") from exc
    owner = next(
        (user for secret, user in configured.items() if hmac.compare_digest(token, secret)), None
    )
    if not isinstance(owner, str) or not USER_ID.fullmatch(owner):
        raise AuthError("Authentication is required.")
    return owner


def enforce_rate_limit(owner_id: str, *, limit: int = 10, window_seconds: int = 60) -> None:
    """Enforce a process-local development limit behind the production edge limit."""
    now = time.monotonic()
    requests = _REQUESTS[owner_id]
    # Cloud Armor or the authenticated task gateway must enforce the global,
    # multi-instance limit; this defense prevents simple local bursts.
    while requests and requests[0] <= now - window_seconds:
        requests.popleft()
    if len(requests) >= limit:
        raise AuthError("The request rate limit was reached.")
    requests.append(now)


def authorize(owner_id: str, state: dict) -> None:
    """Reject access unless the authenticated principal owns the run snapshot."""
    if state.get("owner_id") != owner_id:
        # Do not disclose whether a guessed run identifier exists.
        raise AuthError("The experiment run was not found.")
