"""Modular external repository client; persistence and entry points stay independent."""

import asyncio

from tinypods_core.repositories.helm import HelmRepositoryClient
from tinypods_core.repositories.oci import OCIRepositoryClient
from tinypods_core.repositories.sources import SourceError, SourceSnapshot, repository_url
from tinypods_core.repositories.transport import MetadataHTTP


class RepositoryClient:
    """Typed facade over the Helm-index and OCI discovery adapters.

    This client currently reads external metadata only. It neither stores charts
    nor exposes unused install/render/upgrade operations.
    """

    def __init__(self, http: MetadataHTTP) -> None:
        """Compose adapters around one explicit network policy."""
        self.http = http
        self.helm = HelmRepositoryClient(http)
        self.oci = OCIRepositoryClient(http)

    async def fetch(
        self, source_type: str, url: str, known_charts: tuple[str, ...] = ()
    ) -> SourceSnapshot:
        """Read a complete source snapshot under one operation-wide deadline."""
        try:
            url = repository_url(source_type, url)
        except ValueError:
            raise SourceError(422, "invalid_repository_url") from None
        try:
            async with asyncio.timeout(self.http.settings.repository_timeout_seconds):
                if source_type == "helm_repository":
                    return await self.helm.fetch(url)
                return await self.oci.fetch(url, known_charts)
        except TimeoutError:
            raise SourceError(504, "source_timeout") from None
