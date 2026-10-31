"""Provider-independent session identity and application logout routes."""

from fastapi import APIRouter, Request, Response
from tinypods_api.auth.contracts import (
    IdentityResponse,
    LogoutResponse,
)
from tinypods_api.auth.cookies import request_cookie_policy
from tinypods_api.auth.dependencies import (
    CSRF_HEADER,
    SESSION_CSRF_METADATA,
    PrincipalDependency,
    SessionDependency,
    check_origin,
)
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import RateLimitedRoute

router = APIRouter(route_class=RateLimitedRoute)


@router.get(
    "/session",
    operation_id=build_operation_id("auth", "session", "get"),
    summary="Show the signed-in account",
    response_model=IdentityResponse,
)
async def session_identity(principal: PrincipalDependency) -> IdentityResponse:
    """Show the account currently signed in to this browser."""
    return IdentityResponse(
        user_id=principal.user_id, username=principal.username, is_owner=principal.is_owner
    )


@router.post(
    "/logout",
    operation_id=build_operation_id("auth", "session", "logout"),
    summary="Sign out of TinyPods",
    response_model=LogoutResponse,
    openapi_extra=SESSION_CSRF_METADATA,
    responses={
        200: {"description": "This browser is signed out of TinyPods."},
        401: {"description": "Sign in before using this action."},
        403: {"description": "This sign-out request could not be verified."},
    },
)
async def logout(
    request: Request, response: Response, service: SessionDependency
) -> LogoutResponse:
    """Sign this browser out of TinyPods.

    Other browsers stay signed in. Signing out here does not sign you out of an
    external identity provider.
    """
    check_origin(request)
    policy = request_cookie_policy(request)
    await service.logout_browser(
        request.cookies.get(policy.session, ""),
        request.headers.get(CSRF_HEADER, ""),
        request.cookies.get(policy.refresh),
    )
    for name in (policy.session, policy.csrf, policy.refresh):
        response.delete_cookie(
            name, secure=policy.secure, httponly=name != policy.csrf, samesite="strict", path="/"
        )
    return LogoutResponse()
