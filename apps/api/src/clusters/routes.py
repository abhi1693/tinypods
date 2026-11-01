"""Standard version-one region and cluster registry resources."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from tinypods_api.auth import PrincipalDependency
from tinypods_api.clusters import dependencies
from tinypods_api.clusters.contracts import (
    ClusterPatch,
    ClusterQuery,
    ClusterRead,
    ClusterWrite,
    RegionPatch,
    RegionQuery,
    RegionRead,
    RegionWrite,
)
from tinypods_api.http import Page, ResourceAction, ResourceRouter
from tinypods_core.applications.service import DefinitionService
from tinypods_core.clusters import Cluster, Region
from tinypods_core.clusters.service import RegionService

router = APIRouter()
Regions = Annotated[RegionService, Depends(dependencies.regions)]
Clusters = Annotated[DefinitionService[Cluster], Depends(dependencies.clusters)]


async def region_read(service: RegionService, record: Region) -> RegionRead:
    """Resolve inherited presentation metadata without changing stored overrides."""
    return RegionRead.model_validate((await service.resolve_flags([record]))[0])


region_router = ResourceRouter(domain="clusters", resource="region", prefix="/regions")


@region_router.action(ResourceAction.LIST, response_model=Page[RegionRead])
async def list_region(
    query: Annotated[RegionQuery, Query()], service: Regions, principal: PrincipalDependency
) -> Page[RegionRead]:
    """Browse an authenticated page with explicit location filters."""
    filters: dict[str, str | UUID | None] = {
        name: value
        for name, value in query.model_dump(include={"slug", "parent_id"}).items()
        if value is not None
    }
    if query.roots:
        filters["parent_id"] = None
    records, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    records = await service.resolve_flags(records)
    return Page(
        items=[RegionRead.model_validate(record) for record in records],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@region_router.action(ResourceAction.RETRIEVE, response_model=RegionRead)
async def retrieve_region(
    id: UUID,
    service: Regions,
    principal: PrincipalDependency,
) -> RegionRead:
    """Read one registry identity with current authentication."""
    record = await service.get(principal, id)
    return await region_read(service, record)


@region_router.action(ResourceAction.CREATE, response_model=RegionRead)
async def create_region(
    payload: RegionWrite,
    service: Regions,
    principal: PrincipalDependency,
) -> RegionRead:
    """Write audited registry metadata; requires instance-owner privileges."""
    record = await service.save(principal, payload.model_dump())
    return await region_read(service, record)


@region_router.action(ResourceAction.REPLACE, response_model=RegionRead)
async def replace_region(
    id: UUID,
    payload: RegionWrite,
    service: Regions,
    principal: PrincipalDependency,
) -> RegionRead:
    """Write audited registry metadata; requires instance-owner privileges."""
    record = await service.save(principal, payload.model_dump(), identity=id)
    return await region_read(service, record)


@region_router.action(ResourceAction.UPDATE, response_model=RegionRead)
async def update_region(
    id: UUID,
    payload: RegionPatch,
    service: Regions,
    principal: PrincipalDependency,
) -> RegionRead:
    """Write audited registry metadata; requires instance-owner privileges."""
    record = await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    return await region_read(service, record)


@region_router.action(ResourceAction.DELETE)
async def delete_region(id: UUID, service: Regions, principal: PrincipalDependency) -> None:
    """Delete an unreferenced registry identity while preserving audit history."""
    await service.delete(principal, id)


router.include_router(region_router)


cluster_router = ResourceRouter(domain="clusters", resource="cluster", prefix="/clusters")


@cluster_router.action(ResourceAction.LIST, response_model=Page[ClusterRead])
async def list_cluster(
    query: Annotated[ClusterQuery, Query()], service: Clusters, principal: PrincipalDependency
) -> Page[ClusterRead]:
    """Browse an authenticated page with explicit location filters."""
    filters: dict[str, str | UUID | None] = {
        name: value
        for name, value in query.model_dump(include={"slug", "region_id"}).items()
        if value is not None
    }
    records, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[ClusterRead.model_validate(record) for record in records],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@cluster_router.action(ResourceAction.RETRIEVE, response_model=ClusterRead)
async def retrieve_cluster(
    id: UUID,
    service: Clusters,
    principal: PrincipalDependency,
) -> ClusterRead:
    """Read one registry identity with current authentication."""
    record = await service.get(principal, id)
    return ClusterRead.model_validate(record)


@cluster_router.action(ResourceAction.CREATE, response_model=ClusterRead)
async def create_cluster(
    payload: ClusterWrite,
    service: Clusters,
    principal: PrincipalDependency,
) -> ClusterRead:
    """Write audited registry metadata; requires instance-owner privileges."""
    record = await service.save(principal, payload.model_dump())
    return ClusterRead.model_validate(record)


@cluster_router.action(ResourceAction.REPLACE, response_model=ClusterRead)
async def replace_cluster(
    id: UUID,
    payload: ClusterWrite,
    service: Clusters,
    principal: PrincipalDependency,
) -> ClusterRead:
    """Write audited registry metadata; requires instance-owner privileges."""
    record = await service.save(principal, payload.model_dump(), identity=id)
    return ClusterRead.model_validate(record)


@cluster_router.action(ResourceAction.UPDATE, response_model=ClusterRead)
async def update_cluster(
    id: UUID,
    payload: ClusterPatch,
    service: Clusters,
    principal: PrincipalDependency,
) -> ClusterRead:
    """Write audited registry metadata; requires instance-owner privileges."""
    record = await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    return ClusterRead.model_validate(record)


@cluster_router.action(ResourceAction.DELETE)
async def delete_cluster(id: UUID, service: Clusters, principal: PrincipalDependency) -> None:
    """Delete an unreferenced registry identity while preserving audit history."""
    await service.delete(principal, id)


router.include_router(cluster_router)
