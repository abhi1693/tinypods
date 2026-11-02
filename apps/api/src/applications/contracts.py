"""Explicit version-one public DTOs for application definitions."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import ConfigDict, Field
from tinypods_api.http import PageParams, RequestDTO
from tinypods_api.http.model_contracts import DefinitionPatch, DefinitionRead
from tinypods_api.http.representations import Related, ResourceRead
from tinypods_api.repositories.contracts import VersionRead
from tinypods_core.applications import ApplicationStatus, ConfigurationField, SourceType
from tinypods_core.applications.contracts import ReleaseOptions, ReleaseValidationIssue
from tinypods_core.applications.definitions import ApplicationState, ReleaseState
from tinypods_core.applications.models import (
    Application,
    ApplicationCategory,
    Category,
    Release,
)


class CategoryWrite(RequestDTO):
    """Complete category state for create and replacement."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    slug: str = Field(max_length=128, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str = ""
    icon: str | None = None


class CategoryRead(DefinitionRead, CategoryWrite):
    """Public category representation."""

    resource_model = Category


class ApplicationWrite(CategoryWrite):
    """Complete application state; status does not enforce billing/access."""

    status: ApplicationStatus = ApplicationStatus.EXPERIMENTAL
    state: ApplicationState = ApplicationState.DRAFT


class ApplicationRead(DefinitionRead, ApplicationWrite):
    """Public application metadata, independent of deployment definitions."""

    resource_model = Application


class ReleaseWrite(ReleaseOptions, RequestDTO):
    """Complete shared Helm definition; no installed values or generated secrets."""

    application_id: UUID = Field(strict=False)
    chart_version_id: UUID = Field(strict=False)
    state: Literal[ReleaseState.DRAFT, ReleaseState.IN_REVIEW] = ReleaseState.DRAFT
    revision: int = Field(default=1, ge=1)
    notes: str = Field(default="", max_length=10000)
    configuration_fields: list[ConfigurationField] = Field(default_factory=list)


class ReleaseRead(DefinitionRead):
    """Public release definition with prefetched application and chart-version summaries."""

    resource_model = Release
    application: Related[ApplicationRead]
    chart_version: Related[VersionRead]
    configuration_fields: list[ConfigurationField]
    revision: int
    state: ReleaseState
    notes: str
    architectures: list[str]
    renderer: str
    renderer_contract: int
    core_version_min: str | None
    core_version_max: str | None
    deprecated: bool
    revoked_at: datetime | None
    lifecycle_reason: str
    published_at: datetime | None


class CategoryPatch(DefinitionPatch):
    """Only supplied category fields are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"\S")
    slug: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str | None = None
    icon: str | None = None


class ApplicationPatch(CategoryPatch):
    """Only supplied application fields are changed."""

    status: ApplicationStatus | None = None
    state: ApplicationState | None = None


class ReleasePatch(DefinitionPatch):
    """Only supplied shared Helm definition fields are changed."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"core_version_min", "core_version_max"})
    application_id: UUID | None = None
    chart_version_id: UUID | None = None
    state: Literal[ReleaseState.DRAFT, ReleaseState.IN_REVIEW] | None = None
    revision: int | None = Field(default=None, ge=1)
    notes: str | None = Field(default=None, max_length=10000)
    architectures: list[Literal["amd64", "arm64", "arm", "ppc64le", "s390x", "riscv64"]] | None = (
        Field(default=None, max_length=6)
    )
    renderer: Literal["helm"] | None = None
    renderer_contract: Literal[1] | None = None
    core_version_min: str | None = Field(default=None, max_length=128)
    core_version_max: str | None = Field(default=None, max_length=128)
    configuration_fields: list[ConfigurationField] | None = None


class CategoryQuery(PageParams):
    """Bounded category queries with explicit search, ordering and exact slug."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["name", "-name", "created", "-created"] = "name"
    slug: str | None = Field(default=None, max_length=128)


class ApplicationQuery(CategoryQuery):
    """Application queries additionally support the single status classification."""

    status: ApplicationStatus | None = None
    state: ApplicationState | None = None


class ReleaseQuery(PageParams):
    """Bounded release queries with exact app/source filters and chart-name search."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["created", "-created", "chart_version", "-chart_version"] = "created"
    application_id: UUID | None = None
    source_type: SourceType | None = None
    chart_version_id: UUID | None = None
    state: ReleaseState | None = None
    deprecated: bool | None = None


class MembershipWrite(RequestDTO):
    """Category identity to associate with the path's application."""

    category_id: UUID


class MembershipRead(ResourceRead):
    """The actual composite membership identity, without synthetic fields."""

    model_config = ConfigDict(from_attributes=True)
    resource_model = ApplicationCategory
    application: Related[ApplicationRead]
    category: Related[CategoryRead]


class ReleaseValidationRead(RequestDTO):
    """Safe validation results without duplicating release definition data."""

    valid: bool
    issues: list[ReleaseValidationIssue]


class ReleaseDeprecation(RequestDTO):
    """Change discoverability metadata while preserving the published release."""

    deprecated: bool = True
    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class ReleaseRevocation(RequestDTO):
    """Record permanent revocation without removing historic desired-state pins."""

    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")
