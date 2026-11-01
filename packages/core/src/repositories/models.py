"""External chart sources and synchronized metadata; chart bytes remain upstream."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlmodel import Field
from tinypods_core.applications.definitions import SourceType
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.audit import capture as _capture  # noqa: F401
from tinypods_core.audit.registry import ModelRegistration, register


class ChartRepository(AuditTrailMixin, table=True):
    """Operator-selected external Helm repository or OCI registry/namespace."""

    __tablename__ = "chart_repositories"
    __table_args__ = (
        UniqueConstraint("source_type", "url", name="uq_chart_repositories_source_url"),
        CheckConstraint("source_type IN ('helm_repository', 'oci_registry')", name="source_type"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
        CheckConstraint("length(trim(url)) > 0", name="url"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(unique=True)
    source_type: SourceType = Field(
        default=SourceType.HELM_REPOSITORY,
        sa_column=Column(
            Enum(
                SourceType,
                native_enum=False,
                values_callable=lambda choices: [choice.value for choice in choices],
            ),
            nullable=False,
        ),
    )
    url: str
    enabled: bool = True
    last_synced_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class Chart(AuditTrailMixin, table=True):
    """Chart name/path within one configured external source."""

    __tablename__ = "charts"
    __table_args__ = (
        UniqueConstraint("repository_id", "name", name="uq_charts_repository_name"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    repository_id: UUID = Field(foreign_key="chart_repositories.id", ondelete="CASCADE", index=True)
    name: str
    available: bool = False


class ChartVersion(AuditTrailMixin, table=True):
    """Immutable observed version/digest pair, preserving pins when upstream changes."""

    __tablename__ = "chart_versions"
    __table_args__ = (
        UniqueConstraint("chart_id", "version", "digest", name="uq_chart_versions_identity"),
        Index(
            "uq_chart_versions_latest", "chart_id", unique=True, postgresql_where=text("is_latest")
        ),
        CheckConstraint("NOT is_latest OR available", name="latest_available"),
        CheckConstraint("length(trim(version)) > 0", name="version"),
        CheckConstraint("digest ~ '^sha256:[0-9a-f]{64}$'", name="digest"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    chart_id: UUID = Field(foreign_key="charts.id", ondelete="CASCADE", index=True)
    version: str = Field(max_length=128)
    digest: str
    description: str = Field(
        default="", sa_column=Column(String, nullable=False, server_default="")
    )
    icon: str | None = Field(default=None, max_length=2048)
    available: bool = False
    is_latest: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default="false")
    )


for _model, _fields, _related in (
    (ChartRepository, ("id", "name", "source_type", "url", "enabled", "last_synced_at"), None),
    (Chart, ("id", "repository_id", "name", "available"), ("repository_id", ChartRepository)),
    (
        ChartVersion,
        ("id", "chart_id", "version", "digest", "description", "icon", "available", "is_latest"),
        ("chart_id", Chart),
    ),
):
    register(ModelRegistration(_model, "repositories", _model.__name__, _fields, related=_related))
