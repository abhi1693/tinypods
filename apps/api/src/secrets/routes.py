"""Cluster-focused kubeconfig management backed by internal encrypted secret storage."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from tinypods_api.auth import PrincipalDependency
from tinypods_api.http import Page, PageParams, ResourceAction, ResourceRouter
from tinypods_api.http.errors import ERROR_RESPONSES
from tinypods_api.http.operations import build_operation_id
from tinypods_api.http.presentation import Presenter
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_api.secrets.contracts import (
    ConnectionRead,
    KubeconfigRead,
    KubeconfigVersionRead,
    KubeconfigWrite,
)
from tinypods_api.secrets.dependencies import secrets
from tinypods_core.secrets.service import SecretService

router = APIRouter(route_class=RateLimitedRoute)
Secrets = Annotated[SecretService, Depends(secrets)]
versions = ResourceRouter(
    domain="clusters",
    resource="kubeconfigVersion",
    prefix="/clusters/{cluster_id}/kubeconfig/versions",
)
connections = ResourceRouter(domain="clusters", resource="clusterConnection", prefix="/clusters")


def presenter(service: SecretService) -> Presenter:
    """Use the shared nested loader for safe credential metadata."""
    return Presenter(service.session, service.authorization)


@router.get(
    "/clusters/{cluster_id}/kubeconfig",
    response_model=KubeconfigRead,
    operation_id=build_operation_id("clusters", "kubeconfig", "retrieve"),
    openapi_extra=EndpointRatePolicy(name="clustersKubeconfigRetrieve", limit=120).openapi_extra(),
    responses=ERROR_RESPONSES,
    tags=["clusters"],
)
async def get_kubeconfig(
    cluster_id: UUID, service: Secrets, principal: PrincipalDependency
) -> KubeconfigRead:
    """Show this cluster's active credential metadata; requires instance-owner privileges."""
    binding = await service.cluster_binding(principal, cluster_id)
    return (await presenter(service).many(principal, KubeconfigRead, [binding]))[0]


@versions.action(ResourceAction.CREATE, response_model=KubeconfigVersionRead)
async def upload_kubeconfig(
    cluster_id: UUID, payload: KubeconfigWrite, service: Secrets, principal: PrincipalDependency
) -> KubeconfigVersionRead:
    """Upload an initial or replacement kubeconfig; set activate=false to stage it.

    Secret metadata and scope are managed internally. The document is write-only;
    callers only identify the cluster and receive the new version's safe metadata.
    """
    version = await service.save_cluster_version(
        principal,
        cluster_id,
        payload.decoded_payload(),
        activate=payload.activate,
    )
    return (await presenter(service).many(principal, KubeconfigVersionRead, [version]))[0]


@versions.action(ResourceAction.LIST, response_model=Page[KubeconfigVersionRead])
async def list_versions(
    cluster_id: UUID,
    query: Annotated[PageParams, Query()],
    service: Secrets,
    principal: PrincipalDependency,
) -> Page[KubeconfigVersionRead]:
    """Browse this cluster's bounded version history without selecting confidential bytes."""
    rows, total = await service.cluster_versions(
        principal, cluster_id, limit=query.limit, offset=query.offset
    )
    values = await presenter(service).many(principal, KubeconfigVersionRead, rows)
    return Page(items=values, total=total, limit=query.limit, offset=query.offset)


@versions.action(ResourceAction.RETRIEVE, response_model=KubeconfigVersionRead)
async def get_version(
    cluster_id: UUID, id: UUID, service: Secrets, principal: PrincipalDependency
) -> KubeconfigVersionRead:
    """Show metadata for a version belonging to the cluster in the request path."""
    _, version = await service.cluster_version(principal, cluster_id, id)
    return (await presenter(service).many(principal, KubeconfigVersionRead, [version]))[0]


@versions.detail_action("activate", response_model=KubeconfigRead)
async def activate_version(
    cluster_id: UUID, id: UUID, service: Secrets, principal: PrincipalDependency
) -> KubeconfigRead:
    """Activate a non-revoked version of this cluster's kubeconfig without a secret ID."""
    binding = await service.activate_cluster_version(principal, cluster_id, id)
    return (await presenter(service).many(principal, KubeconfigRead, [binding]))[0]


@versions.detail_action("revoke", response_model=KubeconfigVersionRead)
async def revoke_version(
    cluster_id: UUID, id: UUID, service: Secrets, principal: PrincipalDependency
) -> KubeconfigVersionRead:
    """Permanently block resolution of this cluster's version; never select a fallback."""
    version = await service.revoke_cluster_version(principal, cluster_id, id)
    return (await presenter(service).many(principal, KubeconfigVersionRead, [version]))[0]


@connections.detail_action("testConnection", response_model=ConnectionRead)
async def test_connection(
    id: UUID, service: Secrets, principal: PrincipalDependency
) -> ConnectionRead:
    """Verify TLS and Kubernetes identity with this cluster's active kubeconfig."""
    return ConnectionRead.model_validate(asdict(await service.test(principal, id)))


router.include_router(versions)
router.include_router(connections)
