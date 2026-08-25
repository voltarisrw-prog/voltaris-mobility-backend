"""Session transport.

Two ways to carry a session, deliberately:

* **Browsers get httpOnly cookies.** A token in `localStorage` is readable by any
  injected script, so one XSS becomes full account takeover. httpOnly means the
  token is never visible to JavaScript at all. The cost is CSRF exposure, which is
  paid for with a double-submit token below.
* **API and mobile clients get bearer tokens**, returned in the login response body.
  They have no cookie jar and no CSRF surface.

The same login endpoint serves both: it always returns the tokens in the body *and*
sets the cookies. A browser ignores the body; a mobile client ignores the cookies.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

SESSION_COOKIE = "voltaris_session"
REFRESH_COOKIE = "voltaris_refresh"
CSRF_COOKIE = "voltaris_csrf"

#: Refresh cookie is scoped to the one endpoint that consumes it, so it is not
#: attached to every request and cannot be stolen from an unrelated handler.
REFRESH_PATH = "/api/v1/auth/refresh"

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def set_session_cookies(response: Response, *, access: str, refresh: str) -> str:
    """Attach the session to the response and return the CSRF token."""
    settings = get_settings()
    same_site = settings.session_cookie_samesite
    # SameSite=None is meaningless without Secure and browsers reject the pair,
    # so it forces HTTPS regardless of environment.
    secure = settings.is_production or same_site == "none"
    csrf = secrets.token_urlsafe(24)

    response.set_cookie(
        SESSION_COOKIE,
        access,
        httponly=True,
        secure=secure,
        # Never Strict: it drops the cookie on the return leg of the Google OAuth
        # redirect, landing the user signed-out immediately after signing in.
        # Lax by default; None when the frontend is on a different registrable
        # domain. SESSION_COOKIE_SAMESITE controls it.
        samesite=same_site,
        max_age=settings.access_token_ttl_seconds,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        httponly=True,
        secure=secure,
        samesite=same_site,
        max_age=settings.refresh_token_ttl_seconds,
        path=REFRESH_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        # Readable on purpose: the browser must echo it in a header, which a
        # cross-site attacker cannot do because it cannot read the cookie.
        httponly=False,
        secure=secure,
        samesite=same_site,
        max_age=settings.refresh_token_ttl_seconds,
        path="/",
    )
    return csrf


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
    response.delete_cookie(CSRF_COOKIE, path="/")


def extract_token(request: Request, authorization: str | None) -> tuple[str, bool]:
    """Return (token, came_from_cookie).

    The header wins when both are present: an explicit `Authorization` is a
    deliberate act by an API client, while a cookie rides along automatically.
    """
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            return token, False

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie, True

    raise AppError(ErrorCode.AUTHENTICATION_REQUIRED, detail="no bearer token or session cookie")


def enforce_csrf(request: Request) -> None:
    """Double-submit check, for cookie-authenticated mutations only.

    A cross-site attacker can cause the browser to send the cookie, but cannot read
    it, so it cannot set the matching header. Bearer requests skip this: an attacker
    who can set an Authorization header already has the token.
    """
    if request.method not in MUTATING_METHODS:
        return

    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise AppError(
            ErrorCode.FORBIDDEN,
            detail=f"csrf mismatch on {request.method} {request.url.path}",
        )
