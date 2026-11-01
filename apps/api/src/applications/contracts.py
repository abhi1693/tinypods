"""Explicit version-one public DTOs for application definitions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tinypods_api.http import PageParams, RequestDTO
from tinypods_core.applications import ApplicationStatus, ConfigurationField, SourceType


class DefinitionRead(BaseModel):
    """Safe identity and audit timestamps shared by definition responses."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created: datetime
    last_updated: datetime


class CategoryWrite(RequestDTO):
    """Complete category state for create and replacement."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    slug: str = Field(max_length=128, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str = ""
    icon: str | None = None


class CategoryRead(DefinitionRead, CategoryWrite):
    """Public category representation."""


class ApplicationWrite(CategoryWrite):
    """Complete application state; status does not enforce billing/access."""

    status: ApplicationStatus = ApplicationStatus.EXPERIMENTAL


class ApplicationRead(DefinitionRead, ApplicationWrite):
    """Public application metadata, independent of deployment definitions."""


class ReleaseWrite(RequestDTO):
    """Complete shared Helm definition; no installed values or generated secrets."""

    application_id: UUID
    chart_version: str = Field(min_length=1, max_length=128, pattern=r"\S")
    source_type: SourceType = SourceType.HELM_REPOSITORY
    source_location: str = Field(min_length=1, pattern=r"\S")
    chart_name: str = Field(min_length=1, pattern=r"\S")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration_fields: list[ConfigurationField] = Field(default_factory=list)


class ReleaseRead(DefinitionRead, ReleaseWrite):
    """Public release definition without installation state."""


class DefinitionPatch(RequestDTO):
    """Reject explicit nulls except for a nullable icon; preserve omitted fields."""

    @model_validator(mode="after")
    def validate_nulls(self) -> "DefinitionPatch":
        """Distinguish absence from clearing nullable data on partial updates."""
        if any(getattr(self, name) is None for name in self.model_fields_set - {"icon"}):
            raise ValueError("Only icon may be explicitly null")
        return self


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
    chart_version: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"\S")
    source_type: SourceType | None = None
    source_location: str | None = Field(default=None, min_length=1, pattern=r"\S")
    chart_name: str | None = Field(default=None, min_length=1, pattern=r"\S")
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
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


class MembershipWrite(RequestDTO):
    """Category identity to associate with the path's application."""

    category_id: UUID


class MembershipRead(BaseModel):
    """The actual composite membership identity, without synthetic fields."""

    model_config = ConfigDict(from_attributes=True)
    application_id: UUID
    category_id: UUID
