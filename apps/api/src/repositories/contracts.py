"""Public repository/source DTOs; synced records contain metadata, never chart bytes."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from tinypods_api.http import PageParams, RequestDTO
from tinypods_api.http.model_contracts import DefinitionPatch, DefinitionRead
from tinypods_api.http.representations import Related
from tinypods_core.applications import SourceType
from tinypods_core.repositories.models import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.sources import CHART_NAME, repository_url


class RepositoryWrite(RequestDTO):
    """Configure an external source's base URL, not a direct index or uploaded archive."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    source_type: SourceType = SourceType.HELM_REPOSITORY
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_source(self) -> "RepositoryWrite":
        """Require a credential-free source location matching its declared type."""
        self.url = repository_url(self.source_type, self.url)
        return self


class RepositoryPatch(DefinitionPatch):
    """Only supplied source configuration changes; service validates the combined state."""

    name: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"\S")
    source_type: SourceType | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None


class RepositoryRead(DefinitionRead):
    """Public source configuration and last successful metadata refresh."""

    resource_model = ChartRepository
    name: str
    source_type: SourceType
    url: str
    enabled: bool
    last_synced_at: datetime | None


class RepositoryQuery(PageParams):
    """Bounded source listing with explicit search and configuration filters."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["name", "-name", "created", "-created"] = "name"
    source_type: SourceType | None = None
    enabled: bool | None = None


class ChartWrite(RequestDTO):
    """Register a known OCI chart path when the registry cannot discover chart names."""

    repository_id: UUID
    name: str = Field(pattern=CHART_NAME, max_length=255)


class ChartPatch(DefinitionPatch):
    """Correct a chart identity before any version has been observed."""

    repository_id: UUID | None = None
    name: str | None = Field(default=None, pattern=CHART_NAME, max_length=255)


class ChartRead(DefinitionRead):
    """Chart identity and availability reported by the last successful source sync."""

    resource_model = Chart
    repository: Related[RepositoryRead]
    name: str
    available: bool


class ChartQuery(PageParams):
    """Bounded chart listing and exact source/availability filters."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["name", "-name", "created", "-created"] = "name"
    repository_id: UUID | None = None
    available: bool | None = None


class VersionRead(DefinitionRead):
    """Immutable upstream chart/version/digest identity; files remain external."""

    resource_model = ChartVersion
    chart: Related[ChartRead]
    version: str
    digest: str
    description: str
    icon: str | None
    is_latest: bool
    available: bool


class VersionQuery(PageParams):
    """Bounded version listing; version ordering is lexical, not semantic."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["version", "-version", "created", "-created"] = "version"
    chart_id: UUID | None = None
    available: bool | None = None
    is_latest: bool | None = None


class SyncRead(RequestDTO):
    """A complete committed metadata refresh, not a chart content/signature attestation."""

    model_config = {"from_attributes": True, "extra": "forbid"}
    repository: Related[RepositoryRead]
    charts: int
    versions: int
    discovery_supported: bool
    synced_at: datetime
