"""
Dashboard safety: a password on every route, and a server-side lock on LIVE trading.

* ``GOVAWARD_DASH_PASSWORD`` (in .env): when set, every request needs HTTP Basic auth
  with that password (any username). The browser asks once and remembers it.
* ``GOVAWARD_ALLOW_LIVE=yes`` (in .env): the only way LIVE trading can be enabled.
  Without it the dashboard refuses to switch to LIVE, and the trade engine refuses to
  stage or execute LIVE orders even if the database says LIVE. Real money therefore
  needs a deliberate edit on the server, never a click.
"""

from __future__ import annotations

import base64
import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response


def live_allowed() -> bool:
    return os.environ.get("GOVAWARD_ALLOW_LIVE", "").strip().lower() == "yes"


LIVE_LOCKED_MESSAGE = (
    "LIVE trading is locked on this server. To enable it, add GOVAWARD_ALLOW_LIVE=yes to "
    "/root/govaward-alpha/.env and restart the service. Paper mode is unaffected."
)


def _password() -> str:
    return os.environ.get("GOVAWARD_DASH_PASSWORD", "").strip()


def check_basic_auth(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8", "replace")
    except Exception:
        return False
    _, _, supplied = decoded.partition(":")
    return hmac.compare_digest(supplied.encode(), password.encode())


class PasswordMiddleware(BaseHTTPMiddleware):
    """Require the dashboard password on every route (when one is configured)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        password = _password()
        if password and not check_basic_auth(request.headers.get("authorization"), password):
            return PlainTextResponse("Authentication required", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="GovAward"'})
        return await call_next(request)
