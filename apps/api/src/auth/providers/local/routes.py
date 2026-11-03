"""Local account login routes."""

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse
from tinypods_api.auth.contracts import (
    LoginRequest,
    LoginResponse,
    RegistrationRequest,
    RegistrationResponse,
)
from tinypods_api.auth.cookies import request_cookie_policy, set_session_cookies
from tinypods_api.auth.dependencies import (
    CSRF_HEADER,
    LOGIN_CSRF_METADATA,
    AuthDependency,
    RegistrationDependency,
    check_origin,
)
from tinypods_api.http.errors import error_response
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import AuthError, Credentials, LocalPasswordProvider
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
    previous_refresh = request.cookies.get(policy.refresh)
    result = await service.login(
        Credentials(username=body.username, password=body.password),
        previous_digest=digest(previous) if previous is not None else None,
        previous_refresh_digest=digest(previous_refresh) if previous_refresh is not None else None,
    )
    set_session_cookies(response, result, policy)
    return LoginResponse.from_principal(
        result.principal,
        expires_at=result.expires_at,
    )


@router.post(
    "/register",
    operation_id=build_operation_id("auth", "local", "register"),
    summary="Register with a local account",
    status_code=201,
    response_model=RegistrationResponse,
    openapi_extra={
        **EndpointRatePolicy(name="registration", limit=10).openapi_extra(),
        **LOGIN_CSRF_METADATA,
    },
    responses={
        201: {"description": "Account created. Sign in with the new credentials."},
        403: {"description": "Registration is closed or the browser request was not verified."},
        409: {"description": "An account could not be created with those details."},
        429: {"description": "Too many registration attempts."},
    },
)
async def register(
    body: RegistrationRequest,
    request: Request,
    service: RegistrationDependency,
) -> RegistrationResponse | JSONResponse:
    """Register a local account, then sign in with your new credentials.

    Registration must be available for this instance. Provide the sign-in header
    shown in the request parameters. No session is issued by this request.
    """
    check_origin(request)
    if request.headers.get(CSRF_HEADER) != "login":
        raise HTTPException(403)
    credentials = Credentials(username=body.username, password=body.password)
    try:
        user_id = await LocalPasswordProvider(service.database, service.limiter).register(
            credentials
        )
    except AuthError as exc:
        if exc.status != 409:
            raise
        return error_response(
            request,
            409,
            "registration_conflict",
            "Cannot create an account with these details. Sign in or choose another username.",
        )
    return RegistrationResponse.model_validate(
        {
            "user": {"id": user_id, "username": credentials.username, "is_owner": False},
        }
    )
