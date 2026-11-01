"""Authorized, atomic application of externally synchronized chart metadata."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from semver import Version
from sqlmodel import col, select
from tinypods_core.applications.service import DefinitionError
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.models import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.sources import Observation, SourceError


def chart_version(value: str) -> Version:
    """Accept Helm's legacy short numeric versions while retaining SemVer precedence."""
    value = value.removeprefix("v")
    # Older public indexes include labels such as v0.15-alpha.3. Helm coerces
    # omitted minor/patch components to zero; preserve their prerelease suffix.
    parts = re.split(r"(?=[-+])", value, maxsplit=1)
    core = parts[0]
    normalized = core + ".0" * max(0, 3 - len(core.split(".")))
    return Version.parse(normalized + "".join(parts[1:]))


def select_versions(
    observations: tuple[Observation, ...],
    existing: dict[str, tuple[str, ...]],
    initial_versions: int,
) -> tuple[Observation, ...]:
    """Limit first observations per chart, then retain its imported semantic-version floor."""
    grouped: dict[str, list[Observation]] = {}
    selected: list[Observation] = []
    try:
        # Exclude alpha/beta/RC versions before counting the initial window, so
        # prereleases neither consume an N slot nor become a chart's latest row.
        for observation in observations:
            if chart_version(observation.version).prerelease is None:
                grouped.setdefault(observation.chart, []).append(observation)
        for name, items in grouped.items():
            ordered = sorted({chart_version(item.version) for item in items}, reverse=True)
            # Legacy prerelease pins must not set the stable-version import floor.
            saved = tuple(
                value for value in existing.get(name, ()) if chart_version(value).prerelease is None
            )
            # A registered chart with no versions has not been synced yet. Once
            # imported, its oldest retained version prevents historical backfill.
            floor = (
                min(chart_version(value) for value in saved)
                if saved
                else ordered[min(initial_versions, len(ordered)) - 1]
            )
            selected.extend(item for item in items if chart_version(item.version) >= floor)
    except ValueError:
        raise SourceError(422, "invalid_source_metadata") from None
    return tuple(selected)


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

    async def run(
        self, principal: Principal, identity: UUID, initial_versions: int = 3
    ) -> SyncResult:
        """Authorize external access and commit only a complete, still-current observation."""
        if not 1 <= initial_versions <= 10000:
            raise DefinitionError(422)
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
            existing = {
                name: tuple(item.version for item in versions if item.chart_id == chart.id)
                for name, chart in charts.items()
            }
            observations = select_versions(snapshot.versions, existing, initial_versions)

            # Availability describes only the latest successful snapshot. Keep old
            # rows and digests so application release pins never change during sync.
            for chart in charts.values():
                chart.available = False
            for previous in versions:
                previous.available = False
                previous.is_latest = False
            for observation in observations:
                if observation.chart not in charts:
                    chart = Chart(repository_id=identity, name=observation.chart)
                    charts[observation.chart] = chart
                    session.add(chart)
                charts[observation.chart].available = True
            await session.flush()
            # The identity includes the digest: a mutable upstream tag creates a new
            # observation while preserving the previously pinned version record.
            observed = set()
            for observation in observations:
                chart = charts[observation.chart]
                key = (chart.id, observation.version, observation.digest)
                version = by_key.get(key)
                if version is None:
                    version = ChartVersion(
                        chart_id=chart.id, version=observation.version, digest=observation.digest
                    )
                    session.add(version)
                    by_key[key] = version
                version.description = observation.description
                version.icon = observation.icon
                version.available = True
                observed.add(key)
            # Old flags were flushed above before choosing replacements, keeping
            # the unique per-chart latest pointer valid throughout the transaction.
            candidates: dict[UUID, list[ChartVersion]] = {}
            for key in observed:
                version = by_key[key]
                candidates.setdefault(version.chart_id, []).append(version)
            for items in candidates.values():
                # Digest breaks ties deterministically if a source reports multiple
                # payloads at the same semantic version. Only one row is latest.
                latest = max(
                    items, key=lambda item: (chart_version(item.version), item.version, item.digest)
                )
                latest.is_latest = True
            repository.last_synced_at = datetime.now(UTC)
            session.add(repository)
            synced_at = repository.last_synced_at
        return SyncResult(
            identity,
            len({item.chart for item in observations}),
            len(observed),
            snapshot.discovery_supported,
            synced_at,
        )
