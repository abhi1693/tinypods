"""Thin local login, session and logout routes with secure browser cookies."""

from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from tinypods_api.auth.contracts import (
    IdentityResponse,
    LoginRequest,
    LoginResponse,
    LoginStartRequest,
    LoginStartResponse,
    LogoutResponse,
    ProviderDescriptor,
    ProviderLogoutResponse,
    ProvidersResponse,
)
from tinypods_api.auth.dependencies import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthDependency,
    PrincipalDependency,
    SessionDependency,
    check_origin,
)
from tinypods_api.http.errors import error_response
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import AuthError, Credentials, Login
from tinypods_core.auth.federation import BrowserLogin
from tinypods_core.auth.tokens import digest

router = APIRouter(prefix="/auth", tags=["Authentication"], route_class=RateLimitedRoute)


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


OIDC_COOKIE = "__Host-tinypods-oidc"


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


def get_browser_login(request: Request) -> BrowserLogin:
    """Require an explicitly enabled redirect provider; never reactivate local login on failure."""
    service: BrowserLogin | None = request.app.state.browser_login
    if not request.app.state.settings.zitadel_enabled or service is None:
        raise HTTPException(404)
    return service


BrowserDependency = Annotated[BrowserLogin, Depends(get_browser_login)]


@router.get("/providers", operation_id="listAuthProviders", response_model=ProvidersResponse)
async def providers(request: Request) -> ProvidersResponse:
    """List safe configured enabled login methods without authentication or upstream I/O.

    Descriptors remain stable during an upstream outage. Start actions generate fresh
    challenges; this response contains no static OIDC authorization URL or credentials.
    """
    settings = request.app.state.settings
    data = []
    if settings.auth_local_enabled:
        data.append(
            ProviderDescriptor(
                id="local", label="Username and password", type="password", login_path="/auth/login"
            )
        )
    if settings.zitadel_enabled:
        data.append(
            ProviderDescriptor(
                id="zitadel",
                label="Zitadel",
                type="redirect",
                login_path="/auth/providers/zitadel/start",
            )
        )
    return ProvidersResponse(data=data)


@router.post(
    "/providers/zitadel/start",
    operation_id="startZitadelLogin",
    response_model=LoginStartResponse,
    openapi_extra=EndpointRatePolicy(name="oidc_start", limit=10).openapi_extra(),
)
async def start_zitadel(
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
async def finish_zitadel(
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
async def logout_zitadel(
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
    await logout(response, principal, sessions)
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
