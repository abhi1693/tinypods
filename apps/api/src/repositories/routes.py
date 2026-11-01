"""Version-one external source management and synchronized metadata browsing."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from tinypods_api.auth import PrincipalDependency
from tinypods_api.http import Page, ResourceAction, ResourceRouter
from tinypods_api.repositories import dependencies
from tinypods_api.repositories.contracts import (
    ChartPatch,
    ChartQuery,
    ChartRead,
    ChartWrite,
    RepositoryPatch,
    RepositoryQuery,
    RepositoryRead,
    RepositoryWrite,
    SyncRead,
    VersionQuery,
    VersionRead,
)
from tinypods_api.repositories.dependencies import SyncDependency
from tinypods_core.applications.service import DefinitionService
from tinypods_core.repositories import Chart, ChartRepository, ChartVersion

router = APIRouter()


repository_router = ResourceRouter(
    domain="repositories", resource="repository", prefix="/chart-repositories"
)
RepositoryService = Annotated[
    DefinitionService[ChartRepository], Depends(dependencies.repositories)
]


@repository_router.action(ResourceAction.LIST, response_model=Page[RepositoryRead])
async def list_repository(
    query: Annotated[RepositoryQuery, Query()],
    service: RepositoryService,
    principal: PrincipalDependency,
) -> Page[RepositoryRead]:
    """Browse repository metadata with bounded, explicitly declared query fields."""
    filters = {
        key: value
        for key, value in query.model_dump(include={"enabled", "source_type"}).items()
        if value is not None
    }
    rows, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[RepositoryRead.model_validate(row) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@repository_router.action(ResourceAction.RETRIEVE, response_model=RepositoryRead)
async def retrieve_repository(
    id: UUID, service: RepositoryService, principal: PrincipalDependency
) -> RepositoryRead:
    """Read one repository after current credential validation."""
    return RepositoryRead.model_validate(await service.get(principal, id))


@repository_router.action(ResourceAction.CREATE, response_model=RepositoryRead)
async def create_repository(
    payload: RepositoryWrite, service: RepositoryService, principal: PrincipalDependency
) -> RepositoryRead:
    """Create an audited repository; requires instance-owner privileges."""
    return RepositoryRead.model_validate(await service.save(principal, payload.model_dump()))


@repository_router.action(ResourceAction.REPLACE, response_model=RepositoryRead)
async def replace_repository(
    id: UUID, payload: RepositoryWrite, service: RepositoryService, principal: PrincipalDependency
) -> RepositoryRead:
    """Replace writable repository state using the complete input schema."""
    return RepositoryRead.model_validate(
        await service.save(principal, payload.model_dump(), identity=id)
    )


@repository_router.action(ResourceAction.UPDATE, response_model=RepositoryRead)
async def update_repository(
    id: UUID, payload: RepositoryPatch, service: RepositoryService, principal: PrincipalDependency
) -> RepositoryRead:
    """Change supplied repository fields while preserving unspecified values."""
    return RepositoryRead.model_validate(
        await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    )


@repository_router.action(ResourceAction.DELETE)
async def delete_repository(
    id: UUID, service: RepositoryService, principal: PrincipalDependency
) -> None:
    """Delete an unreferenced repository; protected history returns a conflict."""
    await service.delete(principal, id)


@repository_router.detail_action("sync", response_model=SyncRead)
async def sync_repository(
    id: UUID,
    service: SyncDependency,
    principal: PrincipalDependency,
    initial_versions: Annotated[int, Query(ge=1, le=10000)] = 3,
) -> SyncRead:
    """Fetch external metadata and commit a complete, still-current snapshot atomically."""
    return SyncRead.model_validate(await service.run(principal, id, initial_versions))


router.include_router(repository_router)


chart_router = ResourceRouter(domain="repositories", resource="chart", prefix="/charts")
ChartService = Annotated[DefinitionService[Chart], Depends(dependencies.charts)]


@chart_router.action(ResourceAction.LIST, response_model=Page[ChartRead])
async def list_chart(
    query: Annotated[ChartQuery, Query()], service: ChartService, principal: PrincipalDependency
) -> Page[ChartRead]:
    """Browse chart metadata with bounded, explicitly declared query fields."""
    filters = {
        key: value
        for key, value in query.model_dump(include={"available", "repository_id"}).items()
        if value is not None
    }
    rows, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[ChartRead.model_validate(row) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@chart_router.action(ResourceAction.RETRIEVE, response_model=ChartRead)
async def retrieve_chart(
    id: UUID, service: ChartService, principal: PrincipalDependency
) -> ChartRead:
    """Read one chart after current credential validation."""
    return ChartRead.model_validate(await service.get(principal, id))


@chart_router.action(ResourceAction.CREATE, response_model=ChartRead)
async def create_chart(
    payload: ChartWrite, service: ChartService, principal: PrincipalDependency
) -> ChartRead:
    """Create an audited chart; requires instance-owner privileges."""
    return ChartRead.model_validate(await service.save(principal, payload.model_dump()))


@chart_router.action(ResourceAction.REPLACE, response_model=ChartRead)
async def replace_chart(
    id: UUID, payload: ChartWrite, service: ChartService, principal: PrincipalDependency
) -> ChartRead:
    """Replace writable chart state using the complete input schema."""
    return ChartRead.model_validate(
        await service.save(principal, payload.model_dump(), identity=id)
    )


@chart_router.action(ResourceAction.UPDATE, response_model=ChartRead)
async def update_chart(
    id: UUID, payload: ChartPatch, service: ChartService, principal: PrincipalDependency
) -> ChartRead:
    """Change supplied chart fields while preserving unspecified values."""
    return ChartRead.model_validate(
        await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    )


@chart_router.action(ResourceAction.DELETE)
async def delete_chart(id: UUID, service: ChartService, principal: PrincipalDependency) -> None:
    """Delete an unreferenced chart; protected history returns a conflict."""
    await service.delete(principal, id)


router.include_router(chart_router)


version_router = ResourceRouter(domain="repositories", resource="version", prefix="/chart-versions")
VersionService = Annotated[DefinitionService[ChartVersion], Depends(dependencies.versions)]


@version_router.action(ResourceAction.LIST, response_model=Page[VersionRead])
async def list_version(
    query: Annotated[VersionQuery, Query()], service: VersionService, principal: PrincipalDependency
) -> Page[VersionRead]:
    """Browse version metadata with bounded, explicitly declared query fields."""
    filters = {
        key: value
        for key, value in query.model_dump(include={"available", "chart_id", "is_latest"}).items()
        if value is not None
    }
    rows, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[VersionRead.model_validate(row) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@version_router.action(ResourceAction.RETRIEVE, response_model=VersionRead)
async def retrieve_version(
    id: UUID, service: VersionService, principal: PrincipalDependency
) -> VersionRead:
    """Read one version after current credential validation."""
    return VersionRead.model_validate(await service.get(principal, id))


router.include_router(version_router)
