"""Thin local login, session and logout routes with secure browser cookies."""

from fastapi import APIRouter, HTTPException, Request, Response
from tinypods_api.auth.contracts import (
    LoginRequest,
    LoginResponse,
)
from tinypods_api.auth.cookies import set_session_cookies
from tinypods_api.auth.dependencies import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthDependency,
    check_origin,
)
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import Credentials
from tinypods_core.auth.tokens import digest

router = APIRouter(route_class=RateLimitedRoute)


@router.post(
    "/login",
    operation_id="loginLocal",
    response_model=LoginResponse,
    openapi_extra=EndpointRatePolicy(name="login", limit=10).openapi_extra(),
)
async def login(
    body: LoginRequest, request: Request, response: Response, service: AuthDependency
) -> LoginResponse:
    """Sign in with local credentials and issue Secure, SameSite=Strict host-only cookies.

    Send X-CSRF-Token: login and JSON credentials from the API's origin. HTTP 401
    rejects invalid credentials; 429 limits attempts with Retry-After; 503 fails
    closed when storage is unavailable. The session cookie is HttpOnly. Read the
    separate CSRF cookie and send it in X-CSRF-Token on subsequent mutations.
    """
    check_origin(request)
    if request.headers.get(CSRF_HEADER) != "login":
        raise HTTPException(403)
    previous = request.cookies.get(SESSION_COOKIE)
    result = await service.login(
        Credentials(username=body.username, password=body.password),
        previous_digest=digest(previous) if previous is not None else None,
    )
    set_session_cookies(response, result)
    return LoginResponse(
        user_id=result.principal.user_id,
        username=result.principal.username,
        is_owner=result.principal.is_owner,
        expires_at=result.expires_at,
    )
