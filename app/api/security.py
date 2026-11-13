"""Admin protection for endpoints that change config or make human decisions.

A shared API key in the X-Admin-Key header. It is the simplest stand-in for
real auth; in production this check would validate an OAuth/JWT token from
a gateway, and only this one function would change.
"""

import secrets
from typing import Optional

from fastapi import Header, Request

from app.core.errors import Unauthorized


def require_admin(request: Request, x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")) -> None:
    expected = request.app.state.container.settings.admin_api_key
    if expected is None:
        return  # open dev mode, a warning is logged at startup
    # compare_digest takes the same time for any wrong key, so the key
    # cannot be guessed character by character from response timings
    if x_admin_key is None or not secrets.compare_digest(x_admin_key, expected):
        raise Unauthorized("missing or wrong X-Admin-Key header")
