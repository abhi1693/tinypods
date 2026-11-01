"""Four core-owned tables for Helm application definitions."""

from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, UniqueConstraint
from sqlmodel import Field
from tinypods_core.applications.definitions import (
    ApplicationStatus,
    ConfigurationFields,
    SourceType,
)
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.audit import capture as _capture  # noqa: F401
from tinypods_core.audit.registry import ModelRegistration, register
from tinypods_core.json_types import JsonRecord
from tinypods_core.persistence import CoreModel


class Application(AuditTrailMixin, table=True):
    """Mutable application information and a single operator-selected status."""

    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
        CheckConstraint(
            "status IN ('experimental', 'stable', 'premium')",
            name="status",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    slug: str = Field(max_length=128, unique=True)
    description: str = ""
    icon: str | None = None
    status: str = ApplicationStatus.EXPERIMENTAL


class Category(AuditTrailMixin, table=True):
    """Reusable application grouping and its display icon."""

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    slug: str = Field(max_length=128, unique=True)
    description: str = ""
    icon: str | None = None


class ApplicationCategory(CoreModel, table=True):
    """Unique application/category membership with protected foreign keys.

    This pair has no independent object identity; audit capture applies to the
    application, category and release entities, not direct membership writes.
    """

    __tablename__ = "application_categories"
    application_id: UUID = Field(
        primary_key=True, foreign_key="applications.id", ondelete="RESTRICT"
    )
    category_id: UUID = Field(
        primary_key=True, foreign_key="categories.id", ondelete="RESTRICT", index=True
    )


class Release(AuditTrailMixin, table=True):
    """Pin a Helm chart and retain shared configuration field definitions."""

    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint("application_id", "chart_version"),
        CheckConstraint("source_type IN ('helm_repository', 'oci_registry')", name="source_type"),
        CheckConstraint("digest ~ '^sha256:[0-9a-f]{64}$'", name="digest"),
        CheckConstraint(
            "jsonb_typeof(configuration_fields) = 'array'", name="configuration_fields"
        ),
        *(
            CheckConstraint(f"length(trim({name})) > 0", name=name)
            for name in ("chart_version", "source_location", "chart_name")
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    application_id: UUID = Field(foreign_key="applications.id", ondelete="RESTRICT", index=True)
    chart_version: str = Field(max_length=128)
    source_type: str = SourceType.HELM_REPOSITORY
    source_location: str
    chart_name: str
    digest: str
    configuration_fields: list[JsonRecord] = Field(
        default_factory=list, sa_column=Column(ConfigurationFields, nullable=False)
    )


for _model, _fields, _related in (
    (Application, ("id", "name", "slug", "description", "icon", "status"), None),
    (Category, ("id", "name", "slug", "description", "icon"), None),
    (
        Release,
        (
            "id",
            "application_id",
            "chart_version",
            "source_type",
            "source_location",
            "chart_name",
            "digest",
        ),
        ("application_id", Application),
    ),
):
    register(ModelRegistration(_model, "applications", _model.__name__, _fields, related=_related))
