"""Four core-owned tables for Helm application definitions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlmodel import Field, Relationship
from tinypods_core.applications.definitions import (
    ApplicationState,
    ApplicationStatus,
    ConfigurationFields,
    ReleaseState,
)
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.audit import capture as _capture  # noqa: F401
from tinypods_core.audit.registry import ModelRegistration, register
from tinypods_core.json_types import JsonRecord
from tinypods_core.persistence import CoreModel

if TYPE_CHECKING:
    from tinypods_core.repositories.models import ChartVersion


class Application(AuditTrailMixin, table=True):
    """Mutable application information and a single operator-selected status."""

    brief_fields: ClassVar[tuple[str, ...] | None] = (
        "id",
        "name",
        "slug",
        "icon",
        "status",
        "state",
    )

    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
        CheckConstraint("state IN ('draft', 'in_review', 'published')", name="state"),
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
    state: ApplicationState = Field(
        default=ApplicationState.DRAFT,
        sa_column=Column(
            SAEnum(
                ApplicationState,
                values_callable=lambda kind: [member.value for member in kind],
                native_enum=False,
                length=16,
                create_constraint=False,
                validate_strings=True,
            ),
            nullable=False,
            server_default="draft",
        ),
    )


class Category(AuditTrailMixin, table=True):
    """Reusable application grouping and its display icon."""

    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "name", "slug", "icon")

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

    brief_fields: ClassVar[tuple[str, ...] | None] = ("application", "category")
    application: Application = Relationship(
        sa_relationship=relationship("Application", lazy="raise")
    )
    category: Category = Relationship(sa_relationship=relationship("Category", lazy="raise"))

    __tablename__ = "application_categories"
    application_id: UUID = Field(
        primary_key=True, foreign_key="applications.id", ondelete="RESTRICT"
    )
    category_id: UUID = Field(
        primary_key=True, foreign_key="categories.id", ondelete="RESTRICT", index=True
    )


class Release(AuditTrailMixin, table=True):
    """Pin a Helm chart and retain shared configuration field definitions."""

    brief_fields: ClassVar[tuple[str, ...] | None] = (
        "id",
        "application",
        "chart_version",
        "revision",
        "state",
    )
    application: Application = Relationship(
        sa_relationship=relationship("Application", lazy="raise")
    )
    chart_version: ChartVersion = Relationship(
        sa_relationship=relationship("ChartVersion", lazy="raise")
    )

    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "chart_version_id", "revision", name="uq_releases_chart_revision"
        ),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("state IN ('draft', 'in_review', 'published')", name="state"),
        CheckConstraint("state != 'published' OR published_at IS NOT NULL", name="published_at"),
        CheckConstraint("jsonb_typeof(architectures) = 'array'", name="architectures"),
        CheckConstraint(
            "jsonb_typeof(configuration_fields) = 'array'", name="configuration_fields"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    application_id: UUID = Field(foreign_key="applications.id", ondelete="RESTRICT", index=True)
    chart_version_id: UUID = Field(foreign_key="chart_versions.id", ondelete="RESTRICT", index=True)
    revision: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default="1"))
    state: ReleaseState = Field(
        default=ReleaseState.DRAFT,
        sa_column=Column(
            SAEnum(
                ReleaseState,
                values_callable=lambda kind: [member.value for member in kind],
                native_enum=False,
                length=16,
                create_constraint=False,
                validate_strings=True,
            ),
            nullable=False,
            server_default="draft",
        ),
    )
    published_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    notes: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    architectures: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    renderer: str = Field(
        default="helm", sa_column=Column(String, nullable=False, server_default="helm")
    )
    renderer_contract: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default="1")
    )
    core_version_min: str | None = None
    core_version_max: str | None = None
    deprecated: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    revoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    lifecycle_reason: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    configuration_fields: list[JsonRecord] = Field(
        default_factory=list, sa_column=Column(ConfigurationFields, nullable=False)
    )


for _model, _fields, _related in (
    (Application, ("id", "name", "slug", "description", "icon", "status", "state"), None),
    (Category, ("id", "name", "slug", "description", "icon"), None),
    (
        Release,
        (
            "id",
            "application_id",
            "chart_version_id",
            "revision",
            "state",
            "published_at",
            "notes",
            "renderer",
            "renderer_contract",
            "core_version_min",
            "core_version_max",
            "deprecated",
            "revoked_at",
            "lifecycle_reason",
        ),
        ("application_id", Application),
    ),
):
    register(ModelRegistration(_model, "applications", _model.__name__, _fields, related=_related))
