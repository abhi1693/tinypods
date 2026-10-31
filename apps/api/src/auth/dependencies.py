"""HTTP authentication adapters; resource authorization belongs inside services."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from tinypods_core.auth import AuthService, Credentials, Principal, SessionService

SESSION_COOKIE = "__Host-tinypods-session"
CSRF_COOKIE = "__Host-tinypods-csrf"
CSRF_HEADER = "x-csrf-token"


def get_auth(request: Request) -> AuthService[Credentials]:
    """Require the configured core authentication service without fallback."""
    if not request.app.state.settings.auth_local_enabled:
        raise HTTPException(404)
    service: AuthService[Credentials] | None = request.app.state.auth
    if service is None:
        raise HTTPException(503)
    return service


AuthDependency = Annotated[AuthService[Credentials], Depends(get_auth)]


def get_sessions(request: Request) -> SessionService:
    """Require provider-independent sessions for all authenticated feature access."""
    sessions: SessionService | None = request.app.state.sessions
    if sessions is None:
        raise HTTPException(503)
    return sessions


SessionDependency = Annotated[SessionService, Depends(get_sessions)]


def check_origin(request: Request) -> None:
    """Reject foreign browser origins; custom CSRF headers also exclude form submissions."""
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403)


async def get_principal(request: Request, service: SessionDependency) -> Principal:
    """Require a live cookie session and a bound CSRF header for every unsafe method."""
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get(CSRF_HEADER, "")
    return await service.authenticate(token, csrf_token=csrf)


PrincipalDependency = Annotated[Principal, Depends(get_principal)]
