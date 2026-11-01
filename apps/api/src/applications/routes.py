"""Version-one definition handlers; services own authorization and transactions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from tinypods_api.applications import dependencies
from tinypods_api.applications.contracts import (
    ApplicationPatch,
    ApplicationQuery,
    ApplicationRead,
    ApplicationWrite,
    CategoryPatch,
    CategoryQuery,
    CategoryRead,
    CategoryWrite,
    MembershipRead,
    MembershipWrite,
    ReleasePatch,
    ReleaseQuery,
    ReleaseRead,
    ReleaseWrite,
)
from tinypods_api.auth import PrincipalDependency
from tinypods_api.http import Page, PageParams, ResourceAction, ResourceRouter
from tinypods_api.http.presentation import MembershipResource, ResourceService
from tinypods_core.applications import Application, Category, Release

router = APIRouter()


application_router = ResourceRouter(
    domain="applications", resource="application", prefix="/applications"
)
ApplicationService = Annotated[
    ResourceService[Application, ApplicationRead], Depends(dependencies.applications)
]


@application_router.action(ResourceAction.LIST, response_model=Page[ApplicationRead])
async def list_applications(
    query: Annotated[ApplicationQuery, Query()],
    service: ApplicationService,
    principal: PrincipalDependency,
) -> Page[ApplicationRead]:
    """List accessible applications using bounded, explicitly declared query fields."""
    filters = {
        key: value
        for key, value in query.model_dump(include={"status", "slug"}).items()
        if value is not None
    }
    items, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[ApplicationRead.model_validate(item) for item in items],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@application_router.action(ResourceAction.RETRIEVE, response_model=ApplicationRead)
async def retrieve_application(
    id: UUID, service: ApplicationService, principal: PrincipalDependency
) -> ApplicationRead:
    """Retrieve one application after validating current access."""
    return ApplicationRead.model_validate(await service.get(principal, id))


@application_router.action(ResourceAction.CREATE, response_model=ApplicationRead)
async def create_application(
    payload: ApplicationWrite, service: ApplicationService, principal: PrincipalDependency
) -> ApplicationRead:
    """Create an audited application; requires instance-owner privileges."""
    return ApplicationRead.model_validate(await service.save(principal, payload.model_dump()))


@application_router.action(ResourceAction.REPLACE, response_model=ApplicationRead)
async def replace_application(
    id: UUID, payload: ApplicationWrite, service: ApplicationService, principal: PrincipalDependency
) -> ApplicationRead:
    """Replace all writable application fields, applying defaults for omitted optional fields."""
    return ApplicationRead.model_validate(
        await service.save(principal, payload.model_dump(), identity=id)
    )


@application_router.action(ResourceAction.UPDATE, response_model=ApplicationRead)
async def update_application(
    id: UUID, payload: ApplicationPatch, service: ApplicationService, principal: PrincipalDependency
) -> ApplicationRead:
    """Update only supplied application fields; omitted fields retain their values."""
    return ApplicationRead.model_validate(
        await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    )


@application_router.action(ResourceAction.DELETE)
async def delete_application(
    id: UUID, service: ApplicationService, principal: PrincipalDependency
) -> None:
    """Delete an unreferenced application; preserve protected references and audit history."""
    await service.delete(principal, id)


router.include_router(application_router)


category_router = ResourceRouter(domain="applications", resource="category", prefix="/categories")
CategoryService = Annotated[
    ResourceService[Category, CategoryRead], Depends(dependencies.categories)
]


@category_router.action(ResourceAction.LIST, response_model=Page[CategoryRead])
async def list_categories(
    query: Annotated[CategoryQuery, Query()],
    service: CategoryService,
    principal: PrincipalDependency,
) -> Page[CategoryRead]:
    """List accessible categories using bounded, explicitly declared query fields."""
    filters = {
        key: value for key, value in query.model_dump(include={"slug"}).items() if value is not None
    }
    items, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[CategoryRead.model_validate(item) for item in items],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@category_router.action(ResourceAction.RETRIEVE, response_model=CategoryRead)
async def retrieve_category(
    id: UUID, service: CategoryService, principal: PrincipalDependency
) -> CategoryRead:
    """Retrieve one category after validating current access."""
    return CategoryRead.model_validate(await service.get(principal, id))


@category_router.action(ResourceAction.CREATE, response_model=CategoryRead)
async def create_category(
    payload: CategoryWrite, service: CategoryService, principal: PrincipalDependency
) -> CategoryRead:
    """Create an audited category; requires instance-owner privileges."""
    return CategoryRead.model_validate(await service.save(principal, payload.model_dump()))


@category_router.action(ResourceAction.REPLACE, response_model=CategoryRead)
async def replace_category(
    id: UUID, payload: CategoryWrite, service: CategoryService, principal: PrincipalDependency
) -> CategoryRead:
    """Replace all writable category fields, applying defaults for omitted optional fields."""
    return CategoryRead.model_validate(
        await service.save(principal, payload.model_dump(), identity=id)
    )


@category_router.action(ResourceAction.UPDATE, response_model=CategoryRead)
async def update_category(
    id: UUID, payload: CategoryPatch, service: CategoryService, principal: PrincipalDependency
) -> CategoryRead:
    """Update only supplied category fields; omitted fields retain their values."""
    return CategoryRead.model_validate(
        await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    )


@category_router.action(ResourceAction.DELETE)
async def delete_category(
    id: UUID, service: CategoryService, principal: PrincipalDependency
) -> None:
    """Delete an unreferenced category; preserve protected references and audit history."""
    await service.delete(principal, id)


router.include_router(category_router)


release_router = ResourceRouter(domain="applications", resource="release", prefix="/releases")
ReleaseService = Annotated[ResourceService[Release, ReleaseRead], Depends(dependencies.releases)]


@release_router.action(ResourceAction.LIST, response_model=Page[ReleaseRead])
async def list_releases(
    query: Annotated[ReleaseQuery, Query()], service: ReleaseService, principal: PrincipalDependency
) -> Page[ReleaseRead]:
    """List accessible releases using bounded, explicitly declared query fields."""
    filters = {
        key: value
        for key, value in query.model_dump(
            include={"application_id", "source_type", "chart_version_id"}
        ).items()
        if value is not None
    }
    items, total = await service.list(
        principal,
        limit=query.limit,
        offset=query.offset,
        ordering=query.ordering,
        q=query.q,
        filters=filters,
    )
    return Page(
        items=[ReleaseRead.model_validate(item) for item in items],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@release_router.action(ResourceAction.RETRIEVE, response_model=ReleaseRead)
async def retrieve_release(
    id: UUID, service: ReleaseService, principal: PrincipalDependency
) -> ReleaseRead:
    """Retrieve one release after validating current access."""
    return ReleaseRead.model_validate(await service.get(principal, id))


@release_router.action(ResourceAction.CREATE, response_model=ReleaseRead)
async def create_release(
    payload: ReleaseWrite, service: ReleaseService, principal: PrincipalDependency
) -> ReleaseRead:
    """Create an audited release; requires instance-owner privileges."""
    return ReleaseRead.model_validate(await service.save(principal, payload.model_dump()))


@release_router.action(ResourceAction.REPLACE, response_model=ReleaseRead)
async def replace_release(
    id: UUID, payload: ReleaseWrite, service: ReleaseService, principal: PrincipalDependency
) -> ReleaseRead:
    """Replace all writable release fields, applying defaults for omitted optional fields."""
    return ReleaseRead.model_validate(
        await service.save(principal, payload.model_dump(), identity=id)
    )


@release_router.action(ResourceAction.UPDATE, response_model=ReleaseRead)
async def update_release(
    id: UUID, payload: ReleasePatch, service: ReleaseService, principal: PrincipalDependency
) -> ReleaseRead:
    """Update only supplied release fields; omitted fields retain their values."""
    return ReleaseRead.model_validate(
        await service.save(principal, payload.model_dump(exclude_unset=True), identity=id)
    )


@release_router.action(ResourceAction.DELETE)
async def delete_release(id: UUID, service: ReleaseService, principal: PrincipalDependency) -> None:
    """Delete an unreferenced release; preserve protected references and audit history."""
    await service.delete(principal, id)


router.include_router(release_router)


membership_router = ResourceRouter(
    domain="applications", resource="membership", prefix="/applications/{application_id}/categories"
)
MembershipDependency = Annotated[
    MembershipResource[MembershipRead], Depends(dependencies.memberships)
]


@membership_router.action(ResourceAction.LIST, response_model=Page[MembershipRead])
async def list_memberships(
    application_id: UUID,
    page: Annotated[PageParams, Query()],
    service: MembershipDependency,
    principal: PrincipalDependency,
) -> Page[MembershipRead]:
    """List category memberships of an existing application with bounded pagination."""
    rows, total = await service.list(
        principal, application_id, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[MembershipRead.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@membership_router.action(ResourceAction.CREATE, response_model=MembershipRead)
async def add_membership(
    application_id: UUID,
    payload: MembershipWrite,
    service: MembershipDependency,
    principal: PrincipalDependency,
) -> MembershipRead:
    """Associate an existing category with an application; requires owner privileges."""
    return MembershipRead.model_validate(
        await service.add(principal, application_id, payload.category_id)
    )


@membership_router.action(ResourceAction.DELETE)
async def delete_membership(
    application_id: UUID, id: UUID, service: MembershipDependency, principal: PrincipalDependency
) -> None:
    """Remove the category identified by id from this application, preserving both entities."""
    await service.delete(principal, application_id, id)


router.include_router(membership_router)
