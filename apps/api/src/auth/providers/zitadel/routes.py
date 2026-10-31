"""Thin local login, session and logout routes with secure browser cookies."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from tinypods_api.auth.contracts import (
    LoginStartRequest,
    LoginStartResponse,
    ProviderLogoutResponse,
)
from tinypods_api.auth.cookies import set_session_cookies
from tinypods_api.auth.dependencies import (
    CSRF_HEADER,
    SESSION_COOKIE,
    PrincipalDependency,
    SessionDependency,
    check_origin,
)
from tinypods_api.auth.session_routes import logout as logout_session
from tinypods_api.http.errors import error_response
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import AuthError
from tinypods_core.auth.federation import BrowserLogin

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
    operation_id="startZitadelLogin",
    response_model=LoginStartResponse,
    openapi_extra=EndpointRatePolicy(name="oidc_start", limit=10).openapi_extra(),
)
async def start(
    body: LoginStartRequest, request: Request, response: Response, service: BrowserDependency
) -> LoginStartResponse:
    """Start Code+PKCE S256 login after a same-origin custom-header CSRF check.

    Store state/nonce/verifier server-side for five minutes. Only an opaque Secure,
    HttpOnly, SameSite=Lax correlation cookie crosses the top-level callback navigation.
    The application session cookie remains Strict. Reject unsafe return targets (400),
    disabled providers (404), rate limits (429) and unavailable dependencies (503).
    """
    check_origin(request)
    if request.headers.get(CSRF_HEADER) != "login":
        raise HTTPException(403)
    result = await service.start(
        body.return_to,
        request.cookies.get(SESSION_COOKIE),
        browser_token=request.cookies.get(OIDC_COOKIE),
    )
    response.set_cookie(
        OIDC_COOKIE,
        result.browser_token,
        max_age=300,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return LoginStartResponse(authorization_url=result.authorization_url)


@router.get(
    "/providers/zitadel/callback",
    operation_id="finishZitadelLogin",
    response_class=RedirectResponse,
    status_code=303,
    response_model=None,
    responses={
        303: {
            "description": "Login complete; navigate to the stored local path",
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
    """Consume a browser-bound transaction and exchange a one-use code, then redirect locally.

    This GET is bound by state and the separate correlation cookie, not a custom CSRF
    header. Duplicate parameters and mixed code/error fail. No tokens/verifier enter
    browser storage. Signature/claims/access failures create no session or fallback.
    """
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(400)
    if code is not None and error is not None:
        raise HTTPException(400)
    result, return_to = await service.finish(
        state, request.cookies.get(OIDC_COOKIE, ""), code, error=error, response_issuer=iss
    )
    response = RedirectResponse(return_to, status_code=303)
    set_session_cookies(response, result)
    return response


@router.post(
    "/providers/zitadel/logout", operation_id="logoutZitadel", response_model=ProviderLogoutResponse
)
async def logout(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    sessions: SessionDependency,
    service: BrowserDependency,
) -> ProviderLogoutResponse | Response:
    """Revoke the application session first, then return an optional upstream logout target.

    The caller must send its CSRF header. Provider failure never prevents durable local
    revocation. Upstream logout requires browser navigation and may affect all Zitadel
    sessions in that browser; this is not back-channel/global session revocation.
    """
    await logout_session(response, principal, sessions)
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
