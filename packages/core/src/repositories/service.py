"""Authorized, atomic application of externally synchronized chart metadata."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import col, select
from tinypods_core.applications.service import DefinitionError
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.models import Chart, ChartRepository, ChartVersion


@dataclass(frozen=True)
class SyncResult:
    """Successful metadata refresh counts and source-discovery capability."""

    repository_id: UUID
    charts: int
    versions: int
    discovery_supported: bool
    synced_at: datetime


class RepositorySync:
    """Fetch without holding a DB connection, then atomically apply a complete snapshot.

    A row lock and last-updated comparison reject a stale fetch after another
    sync or configuration edit. Failed fetches never hide existing metadata.
    """

    def __init__(
        self, database: Database, authorization: AuthorizationService, source: RepositoryClient
    ) -> None:
        """Retain caller-provided database ownership, policy and bounded source adapter."""
        self.database, self.authorization, self.source = database, authorization, source

    async def run(self, principal: Principal, identity: UUID) -> SyncResult:
        """Authorize external access and commit only a complete, still-current observation."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        async with self.database.session() as session:
            repository = await session.get(ChartRepository, identity)
            if repository is None:
                raise DefinitionError(404)
            if not repository.enabled:
                raise DefinitionError(409)
            source_type, url, generation = (
                repository.source_type,
                repository.url,
                repository.last_updated,
            )
            known = tuple(
                (
                    await session.exec(select(Chart.name).where(Chart.repository_id == identity))
                ).all()
            )
        # Close the read session before network I/O. Slow sources must not reserve
        # an application DB connection or a row lock for their whole download.
        snapshot = await self.source.fetch(source_type, url, known)
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        async with self.database.transaction() as session:
            repository = (
                await session.exec(
                    select(ChartRepository).where(ChartRepository.id == identity).with_for_update()
                )
            ).one_or_none()
            if repository is None:
                raise DefinitionError(404)
            # Another sync/configuration write may have completed during the fetch.
            # Reject this stale result rather than replacing newer observed metadata.
            if repository.last_updated != generation:
                raise DefinitionError(409)
            charts = {
                item.name: item
                for item in (
                    await session.exec(select(Chart).where(Chart.repository_id == identity))
                ).all()
            }
            versions = list(
                (
                    await session.exec(
                        select(ChartVersion)
                        .join(Chart, col(ChartVersion.chart_id) == col(Chart.id))
                        .where(Chart.repository_id == identity)
                    )
                ).all()
            )
            by_key = {(item.chart_id, item.version, item.digest): item for item in versions}
            # Availability describes only the latest successful snapshot. Keep old
            # rows and digests so application release pins never change during sync.
            for chart in charts.values():
                chart.available = False
            for previous in versions:
                previous.available = False
            for observation in snapshot.versions:
                if observation.chart not in charts:
                    chart = Chart(repository_id=identity, name=observation.chart)
                    charts[observation.chart] = chart
                    session.add(chart)
                charts[observation.chart].available = True
            await session.flush()
            # The identity includes the digest: a mutable upstream tag creates a new
            # observation while preserving the previously pinned version record.
            observed = set()
            for observation in snapshot.versions:
                chart = charts[observation.chart]
                key = (chart.id, observation.version, observation.digest)
                version = by_key.get(key)
                if version is None:
                    version = ChartVersion(
                        chart_id=chart.id, version=observation.version, digest=observation.digest
                    )
                    session.add(version)
                    by_key[key] = version
                version.available = True
                observed.add(key)
            repository.last_synced_at = datetime.now(UTC)
            session.add(repository)
            synced_at = repository.last_synced_at
        return SyncResult(
            identity,
            len({item.chart for item in snapshot.versions}),
            len(observed),
            snapshot.discovery_supported,
            synced_at,
        )
