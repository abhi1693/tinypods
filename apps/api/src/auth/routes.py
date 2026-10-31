"""Thin local login, session and logout routes with secure browser cookies."""

from fastapi import APIRouter, Request
from tinypods_api.auth.contracts import (
    ProviderDescriptor,
    ProvidersResponse,
)
from tinypods_api.auth.providers.local.routes import router as local_router
from tinypods_api.auth.providers.zitadel.routes import router as zitadel_router
from tinypods_api.auth.session_routes import router as session_router
from tinypods_api.rate_limit import RateLimitedRoute

router = APIRouter(prefix="/auth", tags=["Authentication"], route_class=RateLimitedRoute)
router.include_router(local_router)
router.include_router(zitadel_router)
router.include_router(session_router)


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
