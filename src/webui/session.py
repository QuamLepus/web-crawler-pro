"""Browser sessions for the console.

The API authenticates with ``Authorization: Bearer <jwt>`` or ``X-API-Key``.
A browser cannot hold either safely in JavaScript, so the console keeps the
JWT in an **httpOnly, SameSite=Lax** cookie and every page renders
server-side, forwarding the token to the API as a normal Bearer header.

Nothing here changes how the public API works — this is a second, browser
shaped front door onto exactly the same endpoints.
"""

from __future__ import annotations

from fastapi import Request, Response

COOKIE = "wcp_session"
MAX_AGE = 60 * 60 * 12  # 12 hours, matching the access token lifetime


def set_session(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=MAX_AGE,
        httponly=True,  # invisible to JavaScript -> XSS cannot steal it
        samesite="lax",  # survives normal navigation, blocks CSRF POSTs
        secure=_secure_by_default(),
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def current_token(request: Request) -> str:
    return request.cookies.get(COOKIE, "")


def _secure_by_default() -> bool:
    """Send ``Secure`` in production; allow plain http for local previews.

    A cookie marked Secure over the http origin used by ``localhost`` previews
    would simply never be stored, and the console would look broken for no
    reason a user could diagnose.
    """
    import os

    return os.getenv("WEBUI_SECURE_COOKIE", "").lower() in ("1", "true", "yes")
