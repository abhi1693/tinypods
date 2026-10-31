"""Thin local login, session and logout routes with secure browser cookies."""

from fastapi import APIRouter, Response
from tinypods_api.auth.contracts import (
    IdentityResponse,
    LogoutResponse,
)
from tinypods_api.auth.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    PrincipalDependency,
    SessionDependency,
)
from tinypods_api.rate_limit import RateLimitedRoute

router = APIRouter(route_class=RateLimitedRoute)


@router.get("/session", operation_id="getSession", response_model=IdentityResponse)
async def session_identity(principal: PrincipalDependency) -> IdentityResponse:
    """Return HTTP 200 with current identity; reject missing, expired or revoked sessions (401)."""
    return IdentityResponse(
        user_id=principal.user_id, username=principal.username, is_owner=principal.is_owner
    )


@router.post("/logout", operation_id="logoutLocal", response_model=LogoutResponse)
async def logout(
    response: Response, principal: PrincipalDependency, service: SessionDependency
) -> LogoutResponse:
    """Revoke this session durably and clear both cookies; require a bound CSRF header.

    HTTP 401 rejects an invalid session, 403 rejects CSRF violations and 503
    reports unavailable storage. Success is returned only after revocation commits.
    """
    await service.logout(principal)
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name, secure=True, httponly=name == SESSION_COOKIE, samesite="strict", path="/"
        )
    return LogoutResponse()
