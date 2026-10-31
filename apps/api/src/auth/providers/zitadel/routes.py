"""Zitadel login initiation, callback and upstream logout routes."""

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.openapi.models import Example
from fastapi.responses import JSONResponse, RedirectResponse
from tinypods_api.auth.contracts import (
    BackchannelErrorResponse,
    LoginResponse,
    LoginStartRequest,
    LoginStartResponse,
    LogoutResponse,
    ProviderLogoutResponse,
)
from tinypods_api.auth.cookies import request_cookie_policy, set_session_cookies
from tinypods_api.auth.dependencies import (
    CSRF_HEADER,
    LOGIN_CSRF_METADATA,
    SESSION_CSRF_METADATA,
    SessionDependency,
    check_origin,
)
from tinypods_api.auth.session_routes import logout as logout_session
from tinypods_api.http.errors import error_response
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import AuthError
from tinypods_core.auth.federation import BrowserLogin
from tinypods_core.auth.providers.zitadel.sessions import ZitadelSessions

EXAMPLES: dict[str, Example] = {
    "identity_provider": Example(
        summary="Choose a configured external sign-in option",
        value={"return_to": "/apps", "identity_provider_id": "configured-provider-id"},
    ),
    "application": Example(
        summary="Return to applications after login", value={"return_to": "/apps"}
    ),
}

router = APIRouter(route_class=RateLimitedRoute)
OIDC_COOKIE = "__Host-tinypods-oidc"


def get_browser_login(request: Request) -> BrowserLogin:
    """Require an explicitly enabled redirect provider; never reactivate local login on failure."""
    service: BrowserLogin | None = request.app.state.browser_login
    if not request.app.state.settings.zitadel_enabled or service is None:
        raise HTTPException(404)
    return service


BrowserDependency = Annotated[BrowserLogin, Depends(get_browser_login)]


@router.post(
    "/providers/zitadel/start",
    operation_id=build_operation_id("auth", "zitadelLogin", "start"),
    summary="Start Zitadel sign-in",
    response_model=LoginStartResponse,
    openapi_extra={
        **EndpointRatePolicy(name="oidc_start", limit=10).openapi_extra(),
        **LOGIN_CSRF_METADATA,
    },
    responses={
        200: {"description": "Open the returned link to sign in with Zitadel."},
        400: {"description": "Choose a page within this installation to return to after sign-in."},
        403: {"description": "This sign-in request could not be verified."},
        429: {
            "description": "Too many sign-in attempts. Wait before trying again.",
            "headers": {"Retry-After": {"schema": {"type": "integer"}}},
        },
        503: {"description": "Zitadel sign-in is temporarily unavailable. Try again later."},
    },
)
async def start(
    body: Annotated[
        LoginStartRequest,
        Body(openapi_examples=EXAMPLES),
    ],
    request: Request,
    response: Response,
    service: BrowserDependency,
) -> LoginStartResponse:
    """Start signing in with Zitadel.

    Open the returned link in the same browser. After signing in, you will return
    to the page chosen in this request. Select an identity provider from the available
    sign-in methods to go directly to that provider, or omit it to use Zitadel’s login page.
    """
    check_origin(request)
    if request.headers.get(CSRF_HEADER) != "login":
        raise HTTPException(403)
    policy = request_cookie_policy(request)
    result = await service.start(
        body.return_to,
        request.cookies.get(policy.session),
        browser_token=request.cookies.get(policy.oidc),
        identity_provider_id=body.identity_provider_id,
        previous_refresh_token=request.cookies.get(policy.refresh),
    )
    response.set_cookie(
        policy.oidc,
        result.browser_token,
        max_age=300,
        secure=policy.secure,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return LoginStartResponse(authorization_url=result.authorization_url)


@router.get(
    "/providers/zitadel/callback",
    operation_id=build_operation_id("auth", "zitadelLogin", "callback"),
    summary="Complete Zitadel sign-in",
    response_class=RedirectResponse,
    status_code=303,
    response_model=None,
    responses={
        303: {
            "description": "Signed in. Continue to the requested page.",
            "headers": {"Location": {"schema": {"type": "string"}}},
        }
    },
)
async def callback(
    request: Request,
    service: BrowserDependency,
    state: Annotated[str, Query(min_length=1, max_length=128)],
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=255)] = None,
    iss: Annotated[str | None, Query(max_length=2048)] = None,
) -> RedirectResponse:
    """Complete sign-in after returning from Zitadel.

    Signs this browser in and sends you to the page chosen when sign-in started.
    The sign-in attempt must still be valid.
    """
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(400)
    if code is not None and error is not None:
        raise HTTPException(400)
    policy = request_cookie_policy(request)
    result, return_to = await service.finish(
        state, request.cookies.get(policy.oidc, ""), code, error=error, response_issuer=iss
    )
    response = RedirectResponse(return_to, status_code=303)
    set_session_cookies(response, result, policy)
    return response


@router.post(
    "/providers/zitadel/logout",
    operation_id=build_operation_id("auth", "zitadel", "logout"),
    summary="Sign out of TinyPods and Zitadel",
    response_model=ProviderLogoutResponse,
    openapi_extra=SESSION_CSRF_METADATA,
    responses={
        200: {
            "description": "Signed out of TinyPods. Open the returned link to sign out of Zitadel."
        },
        503: {"description": "Signed out of TinyPods; Zitadel sign-out is unavailable."},
    },
)
async def logout(
    request: Request,
    response: Response,
    sessions: SessionDependency,
    service: BrowserDependency,
) -> ProviderLogoutResponse | Response:
    """Sign this browser out of TinyPods and get a link to sign out of Zitadel.

    Open the returned link to finish signing out of Zitadel. This can also sign you
    out of other applications using Zitadel in the same browser.
    """
    await logout_session(request, response, sessions)
    try:
        url = await service.provider.logout_url()
    except AuthError as exc:
        failure = error_response(
            request, exc.status, f"http_{exc.status}", "Provider logout unavailable"
        )
        failure.raw_headers.extend(
            (key, value) for key, value in response.raw_headers if key == b"set-cookie"
        )
        return failure
    return ProviderLogoutResponse(logout_url=url)


def get_provider_sessions(request: Request) -> ZitadelSessions:
    """Require explicitly configured Zitadel session lifecycle support."""
    service: ZitadelSessions | None = request.app.state.zitadel_sessions
    if not request.app.state.settings.zitadel_enabled or service is None:
        raise HTTPException(404)
    return service


ProviderSessionDependency = Annotated[ZitadelSessions, Depends(get_provider_sessions)]


@router.post(
    "/providers/zitadel/refresh",
    operation_id=build_operation_id("auth", "zitadelSession", "refresh"),
    summary="Renew your sign-in",
    response_model=LoginResponse,
    openapi_extra={
        **EndpointRatePolicy(name="oidc_refresh", limit=30).openapi_extra(),
        **SESSION_CSRF_METADATA,
    },
    responses={
        401: {"description": "Sign in again to renew this browser session."},
        409: {"description": "A renewal is already in progress for this browser."},
    },
)
async def refresh(
    request: Request, response: Response, service: ProviderSessionDependency
) -> LoginResponse:
    """Keep this browser signed in without entering your credentials again.

    Send the current CSRF header. A successful renewal replaces the browser's
    sign-in credentials; expired or reused renewal credentials require a new login.
    """
    check_origin(request)
    policy = request_cookie_policy(request)
    result = await service.refresh(
        request.cookies.get(policy.refresh, ""), request.headers.get(CSRF_HEADER, "")
    )
    set_session_cookies(response, result, policy)
    return LoginResponse(
        user_id=result.principal.user_id,
        username=result.principal.username,
        is_owner=result.principal.is_owner,
        expires_at=result.expires_at,
        refresh_expires_at=result.refresh_expires_at,
    )


@router.post(
    "/providers/zitadel/backchannel-logout",
    operation_id=build_operation_id("auth", "zitadelBackchannel", "logout"),
    summary="Receive a Zitadel sign-out notification",
    response_model=LogoutResponse,
    openapi_extra={
        **EndpointRatePolicy(name="oidc_backchannel", limit=300).openapi_extra(),
        "requestBody": {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "required": ["logout_token"],
                        "properties": {"logout_token": {"type": "string", "maxLength": 65536}},
                    },
                }
            },
        },
    },
    responses={
        400: {
            "model": BackchannelErrorResponse,
            "description": "The sign-out notification could not be verified.",
        }
    },
)
async def backchannel_logout(
    request: Request, service: ProviderSessionDependency
) -> LogoutResponse | Response:
    """Sign out the browsers identified by a verified notification from Zitadel.

    This endpoint is called by Zitadel directly and does not require browser cookies.
    """
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/x-www-form-urlencoded"
    ):
        return invalid_logout_response()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 70_000:
            return invalid_logout_response()
        body.extend(chunk)
    try:
        values = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=16)
    except (ValueError, UnicodeError):
        return invalid_logout_response()
    tokens = values.get("logout_token", [])
    if len(tokens) != 1:
        return invalid_logout_response()
    try:
        await service.backchannel_logout(tokens[0])
    except AuthError as exc:
        if exc.status == 400:
            return invalid_logout_response()
        raise
    return LogoutResponse()


def invalid_logout_response() -> JSONResponse:
    """Return the logout protocol's fixed error shape without provider-controlled details."""
    return JSONResponse({"error": "invalid_request"}, status_code=400)
