"""Secure browser session and CSRF cookie issuance."""

from dataclasses import dataclass
from datetime import UTC

from fastapi import Request, Response
from tinypods_core.auth import Login
from tinypods_core.settings import Settings


@dataclass(frozen=True)
class CookiePolicy:
    """Keep cookie names and transport attributes consistent for one environment."""

    secure: bool
    session: str
    csrf: str
    oidc: str
    refresh: str


def cookie_policy(settings: Settings) -> CookiePolicy:
    """Use HTTPS-only cookie names in production and unprefixed development names."""
    secure = settings.environment == "production"
    prefix = "__Host-" if secure else ""
    return CookiePolicy(
        secure,
        prefix + "tinypods-session",
        prefix + "tinypods-csrf",
        prefix + "tinypods-oidc",
        prefix + "tinypods-refresh",
    )


def request_cookie_policy(request: Request) -> CookiePolicy:
    """Resolve cookie behavior from validated application settings."""
    return cookie_policy(request.app.state.settings)


def set_session_cookies(response: Response, result: Login, policy: CookiePolicy) -> None:
    """Set rotated application/CSRF cookies independently of the callback correlation cookie."""
    for name, value in ((policy.session, result.token), (policy.csrf, result.csrf_token)):
        response.set_cookie(
            name,
            value,
            secure=policy.secure,
            httponly=name == policy.session,
            samesite="strict",
            path="/",
            expires=(
                result.expires_at
                if name == policy.session
                else (result.refresh_expires_at or result.expires_at)
            ).astimezone(UTC),
        )

    if result.refresh_token is not None and result.refresh_expires_at is not None:
        response.set_cookie(
            policy.refresh,
            result.refresh_token,
            secure=policy.secure,
            httponly=True,
            samesite="strict",
            path="/",
            expires=result.refresh_expires_at.astimezone(UTC),
        )
    else:
        response.delete_cookie(
            policy.refresh, secure=policy.secure, httponly=True, samesite="strict", path="/"
        )
