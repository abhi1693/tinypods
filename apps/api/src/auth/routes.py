"""Shared authentication routes and configured provider router composition."""

from fastapi import APIRouter, Request
from tinypods_api.auth.contracts import (
    IdentityProviderDescriptor,
    ProviderDescriptor,
    ProvidersResponse,
)
from tinypods_api.auth.providers.local.routes import router as local_router
from tinypods_api.auth.providers.zitadel.routes import router as zitadel_router
from tinypods_api.auth.session_routes import router as session_router
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import RateLimitedRoute
from tinypods_core.settings import Settings

router = APIRouter(route_class=RateLimitedRoute)
router.include_router(session_router)


@router.get(
    "/providers",
    operation_id=build_operation_id("auth", "providers", "list"),
    summary="List available sign-in methods",
    response_model=ProvidersResponse,
    response_model_exclude_none=True,
)
async def providers(request: Request) -> ProvidersResponse:
    """Show the sign-in methods available for this installation.

    Use this list to display a local account form or an external sign-in button.
    """
    settings = request.app.state.settings
    data = []
    if settings.auth_local_enabled:
        data.append(
            ProviderDescriptor(
                id="local",
                label=settings.auth_local_label,
                type="password",
                icon_url=settings.auth_local_icon_url,
                login_path="/auth/login",
            )
        )
    if settings.zitadel_enabled:
        data.append(
            ProviderDescriptor(
                id="zitadel",
                label=settings.zitadel_label,
                icon_url=settings.zitadel_icon_url,
                identity_providers=[
                    IdentityProviderDescriptor(
                        id=provider.id,
                        type=provider.type,
                        label=provider.label,
                        icon_url=provider.icon_url,
                    )
                    for provider in settings.zitadel_identity_providers
                ],
                type="redirect",
                login_path="/auth/providers/zitadel/start",
            )
        )
    return ProvidersResponse(data=data)


def build_router(settings: Settings, *, include_disabled_providers: bool = False) -> APIRouter:
    """Register enabled providers only; the offline exporter can enumerate supported contracts."""
    configured = APIRouter(prefix="/auth", tags=["Authentication"], route_class=RateLimitedRoute)
    configured.include_router(router)
    if include_disabled_providers or settings.auth_local_enabled:
        configured.include_router(local_router)
    if include_disabled_providers or settings.zitadel_enabled:
        configured.include_router(zitadel_router)
    return configured
