"""Exact external chart retrieval with immutable digest pins and safe inspection."""

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import yaml  # type: ignore[import-untyped]
from sqlmodel import col, select
from tinypods_core.applications.service import DefinitionError
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories.archive import MAX_ARCHIVE_BYTES, ChartFacts, inspect_chart
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.helm import parse_index
from tinypods_core.repositories.models import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.transport import Document


@dataclass(frozen=True)
class ChartPin:
    """Detached source identity captured before external I/O."""

    source_type: str
    url: str
    name: str
    version: str
    digest: str


def archive_url(body: bytes, name: str, version: str, digest: str, base: str, maximum: int) -> str:
    """Choose only the exact index entry matching the stored version and digest."""
    observations = parse_index(body, maximum)
    if not any(
        item.chart == name and item.version == version and item.digest == digest
        for item in observations
    ):
        raise SourceError(409, "pinned_chart_unavailable")
    # parse_index has already bounded YAML events and rejected aliases/deep trees.
    index = yaml.safe_load(body)
    candidates = []
    for item in index["entries"][name]:
        reported = item["digest"]
        if not reported.startswith("sha256:"):
            reported = "sha256:" + reported
        if item["version"] == version and reported == digest:
            urls = item.get("urls")
            if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
                raise SourceError(422, "invalid_source_metadata")
            candidates.append(urljoin(base + "/", urls[0]))
    if len(set(candidates)) != 1:
        raise SourceError(422, "ambiguous_chart_archive")
    return candidates[0]


class ChartVerification:
    """Read a pinned external archive without storing it or holding DB connections."""

    def __init__(
        self, database: Database, authorization: AuthorizationService, source: RepositoryClient
    ) -> None:
        """Retain application-owned policy and external source adapters."""
        self.database, self.authorization, self.source = database, authorization, source

    async def pin(self, identity: UUID) -> ChartPin:
        """Read one detached source pin; disabled repositories cannot trigger network access."""
        async with self.database.session() as session:
            row = (
                await session.exec(
                    select(ChartVersion, Chart, ChartRepository)
                    .join(Chart, col(ChartVersion.chart_id) == col(Chart.id))
                    .join(ChartRepository, col(Chart.repository_id) == col(ChartRepository.id))
                    .where(ChartVersion.id == identity)
                )
            ).one_or_none()
            if row is None:
                raise DefinitionError(404)
            version, chart, repository = row
            if not repository.enabled:
                raise DefinitionError(409)
            return ChartPin(
                repository.source_type, repository.url, chart.name, version.version, version.digest
            )

    async def helm_archive(self, pin: ChartPin) -> Document:
        """Resolve the exact Helm index entry and download its bounded archive."""
        index = await self.source.http.get(pin.url + "/index.yaml")
        if index.status != 200:
            raise SourceError(502, "source_unavailable")
        archive_location = await asyncio.to_thread(
            archive_url,
            index.body,
            pin.name,
            pin.version,
            pin.digest,
            index.url.rsplit("/", 1)[0],
            self.source.http.settings.repository_max_items,
        )
        return await self.source.http.get(archive_location, maximum_bytes=MAX_ARCHIVE_BYTES)

    async def oci_archive(self, pin: ChartPin) -> Document:
        """Download the registered OCI chart layer by its immutable content digest."""
        registry = urlsplit(pin.url)
        chart_path = "/".join(part for part in (registry.path.strip("/"), pin.name) if part)
        archive_location = (
            "https://" + registry.netloc + "/v2/" + chart_path + "/blobs/" + pin.digest
        )
        # Sync pins the chart layer, not a mutable tag or manifest. Registry
        # authentication and redirect boundaries remain owned by the OCI adapter.
        return await self.source.oci.registry_get(
            archive_location,
            "repository:" + chart_path + ":pull",
            maximum_bytes=MAX_ARCHIVE_BYTES,
        )

    async def download_archive(self, pin: ChartPin) -> bytes:
        """Select the source adapter and require successful exact-content retrieval."""
        if pin.source_type == "helm_repository":
            archive = await self.helm_archive(pin)
        else:
            archive = await self.oci_archive(pin)
        if archive.status != 200:
            raise SourceError(409, "pinned_chart_unavailable")
        return archive.body

    async def fetch(self, pin: ChartPin) -> ChartFacts:
        """Download first, then verify and inspect bounded bytes off the event loop."""
        archive_bytes = await self.download_archive(pin)
        return await asyncio.to_thread(
            inspect_chart, archive_bytes, pin.name, pin.version, pin.digest
        )

    async def run(self, principal: Principal, identity: UUID) -> ChartFacts:
        """Authorize before/after bounded I/O and reject a changed or removed source."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        before = await self.pin(identity)
        try:
            async with asyncio.timeout(self.source.http.settings.repository_timeout_seconds):
                result = await self.fetch(before)
        except TimeoutError:
            raise SourceError(504, "source_timeout") from None
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        if await self.pin(identity) != before:
            raise DefinitionError(409)
        return result
