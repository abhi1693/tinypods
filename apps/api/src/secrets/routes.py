"""Versioned owner-only credential metadata, lifecycle and manual cluster checks."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from tinypods_api.auth import PrincipalDependency
from tinypods_api.http import Page, ResourceAction, ResourceRouter
from tinypods_api.http.presentation import Presenter
from tinypods_api.secrets.contracts import (
    ActivateWrite,
    ClusterSecretQuery,
    ClusterSecretRead,
    ClusterSecretWrite,
    ConnectionRead,
    SecretVersionQuery,
    SecretVersionRead,
    SecretVersionWrite,
)
from tinypods_api.secrets.dependencies import secrets
from tinypods_core.secrets.models import ClusterSecret, SecretVersion
from tinypods_core.secrets.service import SecretService

router = APIRouter()
Secrets = Annotated[SecretService, Depends(secrets)]
bindings = ResourceRouter(domain="secrets", resource="clusterSecret", prefix="/cluster-secrets")
versions = ResourceRouter(domain="secrets", resource="secretVersion", prefix="/secret-versions")
connections = ResourceRouter(domain="clusters", resource="clusterConnection", prefix="/clusters")


def presenter(service: SecretService) -> Presenter:
    """Reuse the declarative nested loader without introducing a second serializer."""
    return Presenter(service.session, service.authorization)


@bindings.action(ResourceAction.LIST, response_model=Page[ClusterSecretRead])
async def list_bindings(
    query: Annotated[ClusterSecretQuery, Query()], service: Secrets, principal: PrincipalDependency
) -> Page[ClusterSecretRead]:
    """Browse cluster credential metadata; requires instance-owner privileges."""
    rows, total = await service.bindings(
        principal, limit=query.limit, offset=query.offset, cluster_id=query.cluster_id
    )
    values = await presenter(service).many(principal, ClusterSecretRead, rows)
    return Page(items=values, total=total, limit=query.limit, offset=query.offset)


@bindings.action(ResourceAction.RETRIEVE, response_model=ClusterSecretRead)
async def get_binding(
    id: UUID, service: Secrets, principal: PrincipalDependency
) -> ClusterSecretRead:
    """Show the associated secret and active-version metadata without resolving plaintext."""
    row = await service.get(principal, ClusterSecret, id)
    return (await presenter(service).many(principal, ClusterSecretRead, [row]))[0]


@bindings.action(ResourceAction.CREATE, response_model=ClusterSecretRead)
async def create_binding(
    payload: ClusterSecretWrite, service: Secrets, principal: PrincipalDependency
) -> ClusterSecretRead:
    """Encrypt a self-contained kubeconfig and atomically create its cluster association."""
    row = await service.create(
        principal,
        payload.cluster_id,
        payload.name,
        payload.description,
        payload.kubeconfig.get_secret_value().encode(),
    )
    return (await presenter(service).many(principal, ClusterSecretRead, [row]))[0]


@bindings.detail_action("activate", response_model=ClusterSecretRead)
async def activate_binding(
    id: UUID, payload: ActivateWrite, service: Secrets, principal: PrincipalDependency
) -> ClusterSecretRead:
    """Select an existing non-revoked credential version in this exact cluster secret."""
    row = await service.activate(principal, id, payload.version_id)
    return (await presenter(service).many(principal, ClusterSecretRead, [row]))[0]


@versions.action(ResourceAction.LIST, response_model=Page[SecretVersionRead])
async def list_versions(
    query: Annotated[SecretVersionQuery, Query()], service: Secrets, principal: PrincipalDependency
) -> Page[SecretVersionRead]:
    """Browse owner-only logical version history; encrypted bytes are not selected."""
    rows, total = await service.versions(
        principal, limit=query.limit, offset=query.offset, secret_id=query.secret_id
    )
    values = await presenter(service).many(principal, SecretVersionRead, rows)
    return Page(items=values, total=total, limit=query.limit, offset=query.offset)


@versions.action(ResourceAction.RETRIEVE, response_model=SecretVersionRead)
async def get_version(
    id: UUID, service: Secrets, principal: PrincipalDependency
) -> SecretVersionRead:
    """Read version identity, encryption-key identifier and revocation metadata only."""
    row = await service.get(principal, SecretVersion, id)
    return (await presenter(service).many(principal, SecretVersionRead, [row]))[0]


@versions.action(ResourceAction.CREATE, response_model=SecretVersionRead)
async def create_version(
    payload: SecretVersionWrite, service: Secrets, principal: PrincipalDependency
) -> SecretVersionRead:
    """Append a replacement credential; set activate=false to stage it."""
    row = await service.replace(
        principal,
        payload.secret_id,
        payload.kubeconfig.get_secret_value().encode(),
        activate=payload.activate,
    )
    return (await presenter(service).many(principal, SecretVersionRead, [row]))[0]


@versions.detail_action("revoke", response_model=SecretVersionRead)
async def revoke_version(
    id: UUID, service: Secrets, principal: PrincipalDependency
) -> SecretVersionRead:
    """Permanently block future resolution; existing Kubernetes credentials remain external."""
    row = await service.revoke(principal, id)
    return (await presenter(service).many(principal, SecretVersionRead, [row]))[0]


@connections.detail_action("testConnection", response_model=ConnectionRead)
async def test_connection(
    id: UUID, service: Secrets, principal: PrincipalDependency
) -> ConnectionRead:
    """Check verified TLS and authenticated Kubernetes identity using the active credential.

    Requires instance-owner privileges. The bounded request returns fixed diagnostic
    categories rather than upstream bodies. No workload or durable operation is created.
    """
    return ConnectionRead.model_validate(asdict(await service.test(principal, id)))


router.include_router(bindings)
router.include_router(versions)
router.include_router(connections)
