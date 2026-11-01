"""Explicit region and cluster DTOs, without credentials or connection state."""

from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, TypeAdapter, field_validator, model_validator
from tinypods_api.applications.contracts import DefinitionRead
from tinypods_api.http import PageParams, RequestDTO


class RegistryWrite(RequestDTO):
    """Complete human-readable identity."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    slug: str = Field(max_length=128, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str = ""


class RegistryPatch(RequestDTO):
    """Preserve omitted fields and reject clearing required identity values."""

    name: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"\S")
    slug: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str | None = None

    @model_validator(mode="after")
    def validate_nulls(self) -> "RegistryPatch":
        """Allow clearing only a region's parent or flag."""
        if any(
            getattr(self, name) is None for name in self.model_fields_set - {"parent_id", "flag"}
        ):
            raise ValueError("Required registry fields cannot be null")
        return self


class FlagFields(RequestDTO):
    """Optional visual override; null restores inheritance."""

    flag: str | None = Field(default=None, max_length=2048)

    @field_validator("flag")
    @classmethod
    def validate_flag(cls, value: str | None) -> str | None:
        """Validate HTTP(S) visual URLs without making network requests."""
        if value is None:
            return None
        url = TypeAdapter(HttpUrl).validate_python(value)
        if url.username is not None or url.password is not None:
            raise ValueError("Flag URL must not contain credentials")
        return str(url)


class RegionWrite(RegistryWrite, FlagFields):
    """Complete region state; parent may be absent for a root region."""

    parent_id: UUID | None = None


class RegionPatch(RegistryPatch, FlagFields):
    """Reparent a region or change its explicit visual override."""

    parent_id: UUID | None = None


class RegionRead(DefinitionRead):
    """Region metadata; flag includes inheritance without extra public fields."""

    name: str
    slug: str
    description: str
    parent_id: UUID | None
    flag: str | None


class ClusterWrite(RegistryWrite):
    """Register a cluster; name and slug are scoped to its region."""

    region_id: UUID


class ClusterPatch(RegistryPatch):
    """Move or rename a registered cluster with a stable global ID."""

    region_id: UUID | None = None


class ClusterRead(DefinitionRead, ClusterWrite):
    """Registry metadata; kubeconfig is not a cluster response field."""


class RegistryQuery(PageParams):
    """Bounded name search, deterministic ordering and exact slug filter."""

    q: str | None = Field(default=None, max_length=128)
    ordering: Literal["name", "-name", "created", "-created"] = "name"
    slug: str | None = Field(default=None, max_length=128)


class RegionQuery(RegistryQuery):
    """Filter direct children, or explicitly request root regions."""

    parent_id: UUID | None = None
    roots: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> "RegionQuery":
        """Reject contradictory root and direct-parent filters."""
        if self.roots and self.parent_id is not None:
            raise ValueError("Choose roots or a parent region")
        return self


class ClusterQuery(RegistryQuery):
    """Filter clusters directly assigned to a region."""

    region_id: UUID | None = None
