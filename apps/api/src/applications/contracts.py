"""Explicit version-one public DTOs for application definitions."""

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field
from tinypods_api.http import PageParams, RequestDTO
from tinypods_api.http.model_contracts import DefinitionPatch, DefinitionRead
from tinypods_api.http.representations import Related, ResourceRead
from tinypods_api.repositories.contracts import VersionRead
from tinypods_core.applications import ApplicationStatus, ConfigurationField, SourceType
from tinypods_core.applications.models import Application, ApplicationCategory, Category, Release


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


class ApplicationRead(DefinitionRead, ApplicationWrite):
    """Public application metadata, independent of deployment definitions."""

    resource_model = Application


class ReleaseWrite(RequestDTO):
    """Complete shared Helm definition; no installed values or generated secrets."""

    application_id: UUID
    chart_version_id: UUID
    configuration_fields: list[ConfigurationField] = Field(default_factory=list)


class ReleaseRead(DefinitionRead):
    """Public release definition with prefetched application and chart-version summaries."""

    resource_model = Release
    application: Related[ApplicationRead]
    chart_version: Related[VersionRead]
    configuration_fields: list[ConfigurationField]


class CategoryPatch(DefinitionPatch):
    """Only supplied category fields are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"\S")
    slug: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str | None = None
    icon: str | None = None


class ApplicationPatch(CategoryPatch):
    """Only supplied application fields are changed."""

    status: ApplicationStatus | None = None


class ReleasePatch(DefinitionPatch):
    """Only supplied shared Helm definition fields are changed."""

    application_id: UUID | None = None
    chart_version_id: UUID | None = None
    configuration_fields: list[ConfigurationField] | None = None


class CategoryQuery(PageParams):
    """Bounded category queries with explicit search, ordering and exact slug."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["name", "-name", "created", "-created"] = "name"
    slug: str | None = Field(default=None, max_length=128)


class ApplicationQuery(CategoryQuery):
    """Application queries additionally support the single status classification."""

    status: ApplicationStatus | None = None


class ReleaseQuery(PageParams):
    """Bounded release queries with exact app/source filters and chart-name search."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["created", "-created", "chart_version", "-chart_version"] = "created"
    application_id: UUID | None = None
    source_type: SourceType | None = None
    chart_version_id: UUID | None = None


class MembershipWrite(RequestDTO):
    """Category identity to associate with the path's application."""

    category_id: UUID


class MembershipRead(ResourceRead):
    """The actual composite membership identity, without synthetic fields."""

    model_config = ConfigDict(from_attributes=True)
    resource_model = ApplicationCategory
    application: Related[ApplicationRead]
    category: Related[CategoryRead]
