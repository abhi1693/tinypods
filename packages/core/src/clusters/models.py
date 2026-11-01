"""Region hierarchy and Kubernetes cluster identities; credentials are stored separately."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, Index, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import deferred, relationship
from sqlmodel import Field, Relationship
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.audit import capture as _capture  # noqa: F401
from tinypods_core.audit.registry import ModelRegistration, register

# Large private paths are queried in SQL, not loaded into every ORM/API result.
_region_path: Column[list[UUID]] = Column(
    "path", ARRAY(Uuid()), nullable=False, server_default=text("'{}'::uuid[]")
)


class Region(AuditTrailMixin, table=True):
    """Group clusters in an acyclic tree with optional inherited visual metadata."""

    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "name", "slug", "flag")
    parent: Region | None = Relationship(
        sa_relationship=relationship("Region", lazy="raise", remote_side="Region.id")
    )

    __tablename__ = "regions"
    __table_args__ = (
        UniqueConstraint("parent_id", "slug", name="uq_regions_parent_slug"),
        Index(
            "uq_regions_root_slug", "slug", unique=True, postgresql_where=text("parent_id IS NULL")
        ),
        Index("ix_regions_flag_id", "id", postgresql_where=text("flag IS NOT NULL")),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    slug: str = Field(max_length=128)
    description: str = ""
    parent_id: UUID | None = Field(
        default=None, foreign_key="regions.id", ondelete="RESTRICT", index=True
    )
    flag: str | None = Field(default=None, max_length=2048)
    __mapper_args__ = {"properties": {"path": deferred(_region_path)}}
    path: list[UUID] = Field(default_factory=list, sa_column=_region_path)


class Cluster(AuditTrailMixin, table=True):
    """Register one Kubernetes target within a region, without storing kubeconfig here."""

    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "name", "slug", "region")
    region: Region = Relationship(sa_relationship=relationship("Region", lazy="raise"))

    __tablename__ = "clusters"
    __table_args__ = (
        UniqueConstraint("region_id", "slug", name="uq_clusters_region_slug"),
        UniqueConstraint("region_id", "name", name="uq_clusters_region_name"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    slug: str = Field(max_length=128)
    description: str = ""
    region_id: UUID = Field(foreign_key="regions.id", ondelete="RESTRICT", index=True)


for _model, _fields in (
    (Region, ("id", "name", "slug", "description", "parent_id", "flag")),
    (Cluster, ("id", "name", "slug", "description", "region_id")),
):
    register(ModelRegistration(_model, "clusters", _model.__name__, _fields))
