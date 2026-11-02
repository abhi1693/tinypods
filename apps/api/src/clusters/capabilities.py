"""Owner-only cached capability reads with automatic live discovery."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from tinypods_api.auth import PrincipalDependency
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_api.secrets.dependencies import secrets
from tinypods_core.cache.snapshots import SnapshotCache
from tinypods_core.clusters.capabilities import CapabilityRead
from tinypods_core.clusters.discovery import CapabilityService
from tinypods_core.secrets.service import SecretService

router = APIRouter(route_class=RateLimitedRoute, tags=["clusters"])


def capabilities(
    request: Request, credentials: Annotated[SecretService, Depends(secrets)]
) -> CapabilityService:
    """Inject request-owned credentials and the application-owned optional cache."""
    cache = request.app.state.response_cache
    snapshots = (
        SnapshotCache(cache, request.app.state.settings.cache_metadata_ttl_seconds)
        if cache
        else None
    )
    return CapabilityService(credentials, snapshots, request.app.state.discovery_coordinator)


Capabilities = Annotated[CapabilityService, Depends(capabilities)]


@router.get(
    "/clusters/{id}/capabilities",
    response_model=CapabilityRead,
    operation_id=build_operation_id("clusters", "capabilities", "retrieve"),
    openapi_extra=EndpointRatePolicy(name="clustersCapabilitiesRead", limit=30).openapi_extra(),
)
async def get_capabilities(
    id: UUID, service: Capabilities, principal: PrincipalDependency
) -> CapabilityRead:
    """Read cached cluster facts or discover live on a miss or cache failure.

    Requires instance-owner access. Live fallback is bounded and read-only;
    cache failures do not prevent returning fresh partial results.
    """
    return await service.get(principal, id)
