"""Secure browser session and CSRF cookie issuance."""

from datetime import UTC

from fastapi import Response
from tinypods_api.auth.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
)
from tinypods_core.auth import Login


def set_session_cookies(response: Response, result: Login) -> None:
    """Set rotated application/CSRF cookies independently of the callback correlation cookie."""
    for name, value in ((SESSION_COOKIE, result.token), (CSRF_COOKIE, result.csrf_token)):
        response.set_cookie(
            name,
            value,
            secure=True,
            httponly=name == SESSION_COOKIE,
            samesite="strict",
            path="/",
            expires=result.expires_at.astimezone(UTC),
        )
