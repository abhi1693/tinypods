"""Authorized read-through capability discovery with disposable JSON persistence."""

import asyncio
from contextlib import suppress
from datetime import datetime
from uuid import UUID

import anyio
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import select
from tinypods_core.auth import Principal
from tinypods_core.cache.snapshots import SnapshotCache
from tinypods_core.cache.storage import CacheUnavailable
from tinypods_core.clusters.capabilities import (
    CapabilityClient,
    CapabilityRead,
    CapabilitySnapshot,
)
from tinypods_core.secrets.crypto import SecretError
from tinypods_core.secrets.kubernetes import Kubeconfig
from tinypods_core.secrets.models import SecretVersion
from tinypods_core.secrets.service import SecretService


class DiscoveryCoordinator:
    """Bound process-local live discovery without relying on an available Valkey lock."""

    def __init__(self) -> None:
        """Create an event-loop-owned limit shared by request services."""
        self.slots = asyncio.Semaphore(4)


class CapabilityService:
    """Authorize cache hits and source reads independently of cache availability."""

    def __init__(
        self,
        secrets: SecretService,
        cache: SnapshotCache | None,
        coordinator: DiscoveryCoordinator,
        client: CapabilityClient | None = None,
    ) -> None:
        """Inject request-owned credentials and application-owned optional resources."""
        self.secrets = secrets
        self.cache = cache
        self.coordinator = coordinator
        self.client = client or CapabilityClient()

    async def identity(self, principal: Principal, cluster_id: UUID) -> UUID:
        """Resolve the current active, non-revoked credential without loading plaintext."""
        binding = await self.secrets.cluster_binding(principal, cluster_id)
        if binding.active_version_id is None:
            raise SecretError("secret_not_found", 404)
        version = await self.secrets.get(principal, SecretVersion, binding.active_version_id)
        if version.revoked_at is not None:
            raise SecretError("secret_revoked", 409)
        return version.id

    async def get(self, principal: Principal, cluster_id: UUID) -> CapabilityRead:
        """Use a current validated snapshot or fall through to bounded live collection."""
        credential_id = await self.identity(principal, cluster_id)
        await self.secrets.session.rollback()
        if self.cache is not None:
            try:
                body = await self.cache.read(str(cluster_id))
                if body is not None:
                    snapshot = CapabilitySnapshot.model_validate_json(body)
                    if (
                        snapshot.cluster_id == cluster_id
                        and snapshot.credential_version_id == credential_id
                        and await self.identity(principal, cluster_id) == credential_id
                    ):
                        # Revalidate after cache I/O: neither cache possession nor an
                        # old permission/credential snapshot grants access.
                        return CapabilityRead(source="cache", cached=True, snapshot=snapshot)
            except (CacheUnavailable, ValidationError):
                pass
        try:
            with anyio.fail_after(2):
                await self.coordinator.slots.acquire()
        except TimeoutError:
            raise SecretError("cluster_discovery_busy", 503) from None
        try:
            return await self.live(principal, cluster_id)
        finally:
            self.coordinator.slots.release()

    async def live(self, principal: Principal, cluster_id: UUID) -> CapabilityRead:
        """Resolve credentials briefly, release SQL locks, then collect only read-only facts."""
        credential_id = await self.identity(principal, cluster_id)
        binding = await self.secrets.cluster_binding(principal, cluster_id)
        version = await self.secrets._payload_version(credential_id)
        config = Kubeconfig.parse(await self.secrets._resolve(version, binding))
        # The database clock orders refresh starts consistently across API processes.
        started: datetime = (await self.secrets.session.exec(select(func.clock_timestamp()))).one()
        await self.secrets.session.rollback()
        snapshot = await self.client.collect(config, cluster_id, credential_id, started)
        # No Kubernetes call holds a database transaction.
        # An activation, revocation or deletion during collection invalidates publication.
        if await self.identity(principal, cluster_id) != credential_id:
            raise SecretError("cluster_credential_changed", 409)
        cached = False
        if self.cache is not None:
            with suppress(CacheUnavailable):
                cached = await self.cache.publish(
                    str(cluster_id),
                    str(snapshot.model_dump(mode="json")["started_at"]),
                    snapshot.model_dump_json().encode(),
                )
        return CapabilityRead(source="live", cached=cached, snapshot=snapshot)
