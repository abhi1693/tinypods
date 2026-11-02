"""Thin intended-installation, immutable-history and application-secret HTTP routes."""

from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from tinypods_api.auth import PrincipalDependency
from tinypods_api.http import Page, PageParams
from tinypods_api.http.presentation import Presenter
from tinypods_api.http.representations import ResourceRead
from tinypods_api.http.resources import ResourceAction, ResourceRouter
from tinypods_api.installations.contracts import (
    ApplicationSecretCreate,
    ApplicationSecretRead,
    ApplicationSecretVersionCreate,
    ConfigurationInput,
    ConfigurationRead,
    InstallationCreate,
    InstallationPatch,
    InstallationRead,
    RevisionRead,
    SecretVersionRead,
)
from tinypods_api.installations.dependencies import InstallationDependency, SecretDependency
from tinypods_core.applications.installation_service import InstallationService
from tinypods_core.auth import Principal
from tinypods_core.persistence import CoreModel
from tinypods_core.secrets.applications import ApplicationSecretService


async def project[DTO: ResourceRead](
    service: InstallationService | ApplicationSecretService,
    principal: Principal,
    schema: type[DTO],
    records: Sequence[CoreModel],
) -> list[DTO]:
    """Use the shared bounded presenter after core has authorized these records."""
    if isinstance(service, ApplicationSecretService):
        return await Presenter(service.session, service.authorization).many(
            principal, schema, records
        )
    async with service.database.session() as session:
        return await Presenter(session, service.authorization.in_session(session)).many(
            principal, schema, records
        )


async def one[DTO: ResourceRead](
    service: InstallationService | ApplicationSecretService,
    principal: Principal,
    schema: type[DTO],
    record: CoreModel,
) -> DTO:
    """Project one already-authorized result without serializer-side lazy I/O."""
    return (await project(service, principal, schema, [record]))[0]


router = APIRouter()
installations = ResourceRouter(
    domain="applications", resource="installation", prefix="/installations"
)


@installations.action(ResourceAction.CREATE, response_model=InstallationRead)
async def create_installation(
    payload: InstallationCreate, service: InstallationDependency, principal: PrincipalDependency
) -> InstallationRead:
    """Reserve a target for the current owner without deploying cluster resources."""
    return await one(
        service,
        principal,
        InstallationRead,
        await service.create(principal, payload.name, payload.cluster_id, payload.namespace),
    )


@installations.action(ResourceAction.LIST, response_model=Page[InstallationRead])
async def list_installations(
    page: Annotated[PageParams, Query()],
    service: InstallationDependency,
    principal: PrincipalDependency,
) -> Page[InstallationRead]:
    """List this owner's intended installations with deterministic pagination."""
    rows, total = await service.browse(principal, limit=page.limit, offset=page.offset)
    return Page(
        items=await project(service, principal, InstallationRead, rows),
        total=total,
        **page.model_dump(),
    )


@installations.action(ResourceAction.RETRIEVE, response_model=InstallationRead)
async def read_installation(
    id: UUID, service: InstallationDependency, principal: PrincipalDependency
) -> InstallationRead:
    """Read saved installation metadata independently of chart-source availability."""
    return await one(service, principal, InstallationRead, await service.get(principal, id))


@installations.action(ResourceAction.UPDATE, response_model=InstallationRead)
async def rename_installation(
    id: UUID,
    payload: InstallationPatch,
    service: InstallationDependency,
    principal: PrincipalDependency,
) -> InstallationRead:
    """Change the descriptive name while retaining the owner and reserved target."""
    return await one(
        service, principal, InstallationRead, await service.rename(principal, id, payload.name)
    )


@installations.detail_action("validation", response_model=ConfigurationRead)
async def validate_configuration(
    id: UUID,
    payload: ConfigurationInput,
    service: InstallationDependency,
    principal: PrincipalDependency,
) -> ConfigurationRead:
    """Validate exact chart configuration without saving or contacting Kubernetes.

    Secret references receive structural and installation-scope validation.
    This does not check live object existence, keys, capacity or runtime readiness.
    """
    _, result = await service.prepare(
        principal,
        id,
        payload.release_id,
        payload.overrides,
        payload.secrets,
        payload.kubernetes_secrets,
    )
    return ConfigurationRead(valid=not result.issues, issues=list(result.issues))


router.include_router(installations)
revisions = ResourceRouter(
    domain="applications",
    resource="installationRevision",
    prefix="/installations/{installation_id}/revisions",
)


@revisions.action(ResourceAction.CREATE, response_model=RevisionRead)
async def create_revision(
    installation_id: UUID,
    payload: ConfigurationInput,
    service: InstallationDependency,
    principal: PrincipalDependency,
) -> RevisionRead:
    """Append validated immutable history atomically; never advance the deployed pointer.

    Each valid request adds independent history. Validation and external work
    complete before the short atomic write transaction.
    """
    return await one(
        service,
        principal,
        RevisionRead,
        await service.append(
            principal,
            installation_id,
            payload.release_id,
            payload.overrides,
            payload.secrets,
            payload.kubernetes_secrets,
        ),
    )


@revisions.action(ResourceAction.LIST, response_model=Page[RevisionRead])
async def list_revisions(
    installation_id: UUID,
    page: Annotated[PageParams, Query()],
    service: InstallationDependency,
    principal: PrincipalDependency,
) -> Page[RevisionRead]:
    """Read newest-first configuration history.

    Saved configuration remains readable when the source is unavailable or the
    release is revoked.
    """
    rows, total = await service.history(
        principal, installation_id, limit=page.limit, offset=page.offset
    )
    return Page(
        items=await project(service, principal, RevisionRead, rows),
        total=total,
        **page.model_dump(),
    )


@revisions.action(ResourceAction.RETRIEVE, response_model=RevisionRead)
async def read_revision(
    installation_id: UUID, id: UUID, service: InstallationDependency, principal: PrincipalDependency
) -> RevisionRead:
    """Read one immutable configuration belonging to this exact installation."""
    return await one(
        service, principal, RevisionRead, await service.revision(principal, installation_id, id)
    )


router.include_router(revisions)
secrets = ResourceRouter(
    domain="applications",
    resource="installationSecret",
    prefix="/installations/{installation_id}/secrets",
)


@secrets.action(ResourceAction.CREATE, response_model=ApplicationSecretRead)
async def create_secret(
    installation_id: UUID,
    payload: ApplicationSecretCreate,
    service: SecretDependency,
    principal: PrincipalDependency,
) -> ApplicationSecretRead:
    """Encrypt a new application's first version under its real installation scope."""
    scope, _ = await service.save_application_version(
        principal,
        installation_id,
        payload.payload.get_secret_value().encode("utf-8"),
        name=payload.name,
    )
    return await one(service, principal, ApplicationSecretRead, scope)


@secrets.action(ResourceAction.LIST, response_model=Page[ApplicationSecretRead])
async def list_secrets(
    installation_id: UUID,
    page: Annotated[PageParams, Query()],
    service: SecretDependency,
    principal: PrincipalDependency,
) -> Page[ApplicationSecretRead]:
    """List scoped association metadata without resolving confidential bytes."""
    rows, total = await service.application_secrets(
        principal, installation_id, limit=page.limit, offset=page.offset
    )
    return Page(
        items=await project(service, principal, ApplicationSecretRead, rows),
        total=total,
        **page.model_dump(),
    )


router.include_router(secrets)
versions = ResourceRouter(
    domain="applications",
    resource="installationSecretVersion",
    prefix="/installations/{installation_id}/secrets/{secret_id}/versions",
)


@versions.action(ResourceAction.CREATE, response_model=SecretVersionRead)
async def append_secret(
    installation_id: UUID,
    secret_id: UUID,
    payload: ApplicationSecretVersionCreate,
    service: SecretDependency,
    principal: PrincipalDependency,
) -> SecretVersionRead:
    """Append encrypted versioned payload; historical bindings keep their exact IDs."""
    _, version = await service.save_application_version(
        principal,
        installation_id,
        payload.payload.get_secret_value().encode("utf-8"),
        secret_id=secret_id,
    )
    return await one(service, principal, SecretVersionRead, version)


@versions.action(ResourceAction.LIST, response_model=Page[SecretVersionRead])
async def list_secret_versions(
    installation_id: UUID,
    secret_id: UUID,
    page: Annotated[PageParams, Query()],
    service: SecretDependency,
    principal: PrincipalDependency,
) -> Page[SecretVersionRead]:
    """Browse safe metadata for versions in the exact installation and secret."""
    rows, total = await service.application_versions(
        principal, installation_id, secret_id, limit=page.limit, offset=page.offset
    )
    return Page(
        items=await project(service, principal, SecretVersionRead, rows),
        total=total,
        **page.model_dump(),
    )


@versions.action(ResourceAction.RETRIEVE, response_model=SecretVersionRead)
async def read_secret_version(
    installation_id: UUID,
    secret_id: UUID,
    id: UUID,
    service: SecretDependency,
    principal: PrincipalDependency,
) -> SecretVersionRead:
    """Read version metadata without exposing ciphertext or plaintext."""
    return await one(
        service,
        principal,
        SecretVersionRead,
        await service.application_version(principal, installation_id, secret_id, id),
    )


@versions.detail_action("revocation", response_model=SecretVersionRead)
async def revoke_secret_version(
    installation_id: UUID,
    secret_id: UUID,
    id: UUID,
    service: SecretDependency,
    principal: PrincipalDependency,
) -> SecretVersionRead:
    """Permanently revoke new use while retaining existing revision history."""
    return await one(
        service,
        principal,
        SecretVersionRead,
        await service.revoke_application_version(principal, installation_id, secret_id, id),
    )


router.include_router(versions)
