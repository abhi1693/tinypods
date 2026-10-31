"""Local account login routes."""

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.openapi.models import Example
from tinypods_api.auth.contracts import (
    LoginRequest,
    LoginResponse,
)
from tinypods_api.auth.cookies import request_cookie_policy, set_session_cookies
from tinypods_api.auth.dependencies import (
    CSRF_HEADER,
    LOGIN_CSRF_METADATA,
    AuthDependency,
    check_origin,
)
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import Credentials
from tinypods_core.auth.tokens import digest

EXAMPLES: dict[str, Example] = {
    "local": Example(
        summary="Local account login",
        value={"username": "owner", "password": "<your-password-at-least-15-characters>"},
    )
}

router = APIRouter(route_class=RateLimitedRoute)


@router.post(
    "/login",
    operation_id=build_operation_id("auth", "local", "login"),
    summary="Sign in with a local account",
    response_model=LoginResponse,
    openapi_extra={
        **EndpointRatePolicy(name="login", limit=10).openapi_extra(),
        **LOGIN_CSRF_METADATA,
    },
    responses={
        200: {"description": "Signed in. Browser sign-in details have been updated."},
        401: {"description": "The username or password was not accepted."},
        403: {"description": "This sign-in request could not be verified."},
        429: {
            "description": "Too many sign-in attempts. Wait before trying again.",
            "headers": {
                "Retry-After": {
                    "schema": {"type": "integer"},
                    "description": "How long to wait, in seconds.",
                }
            },
        },
        503: {"description": "Sign-in is temporarily unavailable. Try again later."},
    },
)
async def login(
    body: Annotated[
        LoginRequest,
        Body(openapi_examples=EXAMPLES),
    ],
    request: Request,
    response: Response,
    service: AuthDependency,
) -> LoginResponse:
    """Sign in with a local username and password.

    Returns your account details and signs this browser in. Provide the sign-in header
    shown in the request parameters.
    """
    check_origin(request)
    if request.headers.get(CSRF_HEADER) != "login":
        raise HTTPException(403)
    policy = request_cookie_policy(request)
    previous = request.cookies.get(policy.session)
    result = await service.login(
        Credentials(username=body.username, password=body.password),
        previous_digest=digest(previous) if previous is not None else None,
    )
    set_session_cookies(response, result, policy)
    return LoginResponse(
        user_id=result.principal.user_id,
        username=result.principal.username,
        is_owner=result.principal.is_owner,
        expires_at=result.expires_at,
    )
