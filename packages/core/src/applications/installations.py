"""Intended installation identities and immutable validated configuration history."""

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlmodel import Field, Relationship
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.audit.registry import ModelRegistration, register
from tinypods_core.json_types import JsonRecord

if TYPE_CHECKING:
    from tinypods_core.applications.models import Release
    from tinypods_core.clusters import Cluster
    from tinypods_core.db.models import User
    from tinypods_core.secrets.models import Secret, SecretVersion


class ApplicationInstallation(AuditTrailMixin, table=True):
    """Reserve a stable target; revision_id denotes deployed configuration only."""

    owner: "User" = Relationship(sa_relationship=relationship("User", lazy="raise"))
    cluster: "Cluster" = Relationship(sa_relationship=relationship("Cluster", lazy="raise"))
    revision: "InstallationRevision | None" = Relationship(
        sa_relationship=relationship(
            "InstallationRevision",
            lazy="raise",
            viewonly=True,
            foreign_keys="[ApplicationInstallation.revision_id, ApplicationInstallation.id]",
        )
    )
    __tablename__ = "application_installations"
    __table_args__ = (
        Index("ix_installation_owner_history", "owner_id", "created", "id"),
        CheckConstraint("length(trim(name)) > 0", name="name"),
        CheckConstraint(
            "length(namespace) BETWEEN 1 AND 63 AND namespace ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'",
            name="namespace",
        ),
        ForeignKeyConstraint(
            ["revision_id", "id"],
            ["installation_revisions.id", "installation_revisions.installation_id"],
            name="fk_installation_current_revision",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "name", "namespace")
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    owner_id: UUID = Field(foreign_key="auth_user.id", ondelete="RESTRICT")
    cluster_id: UUID = Field(foreign_key="clusters.id", ondelete="RESTRICT", index=True)
    namespace: str = Field(max_length=63)
    revision_id: UUID | None = None

    @property
    def helm_name(self) -> str:
        """Derive the reserved Helm name from immutable installation identity."""
        return "tp-" + self.id.hex


class InstallationRevision(AuditTrailMixin, table=True):
    """Preserve exact release and configuration; saving never advances deployment."""

    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "created")
    installation: ApplicationInstallation = Relationship(
        sa_relationship=relationship(
            "ApplicationInstallation",
            lazy="raise",
            foreign_keys="[InstallationRevision.installation_id]",
        )
    )
    release: "Release" = Relationship(sa_relationship=relationship("Release", lazy="raise"))
    secrets: list["RevisionSecret"] = Relationship(
        sa_relationship=relationship(
            "RevisionSecret",
            lazy="raise",
            viewonly=True,
        )
    )
    kubernetes_secrets: list["RevisionKubernetesSecret"] = Relationship(
        sa_relationship=relationship(
            "RevisionKubernetesSecret",
            lazy="raise",
            viewonly=True,
        )
    )
    __tablename__ = "installation_revisions"
    __table_args__ = (
        UniqueConstraint("id", "installation_id", name="uq_revision_installation"),
        Index("ix_revision_history", "installation_id", "created", "id"),
        CheckConstraint("jsonb_typeof(overrides) = 'object'", name="overrides"),
        CheckConstraint("jsonb_typeof(values) = 'object'", name="values"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    installation_id: UUID = Field(foreign_key="application_installations.id", ondelete="RESTRICT")
    release_id: UUID = Field(foreign_key="releases.id", ondelete="RESTRICT", index=True)
    overrides: JsonRecord = Field(sa_column=Column(JSONB, nullable=False), repr=False)
    values: JsonRecord = Field(sa_column=Column(JSONB, nullable=False), repr=False)


class ApplicationSecret(AuditTrailMixin, table=True):
    """Associate encrypted secret metadata with one real installation target."""

    installation: ApplicationInstallation = Relationship(
        sa_relationship=relationship(
            "ApplicationInstallation",
            lazy="raise",
        )
    )
    secret: "Secret" = Relationship(sa_relationship=relationship("Secret", lazy="raise"))
    __tablename__ = "application_secrets"
    __table_args__ = (Index("ix_application_secret_history", "installation_id", "created", "id"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    secret_id: UUID = Field(foreign_key="secrets.id", ondelete="RESTRICT", unique=True)
    installation_id: UUID = Field(foreign_key="application_installations.id", ondelete="RESTRICT")


class RevisionSecret(AuditTrailMixin, table=True):
    """Pin an encrypted logical version to a configuration target and use."""

    version: "SecretVersion" = Relationship(
        sa_relationship=relationship("SecretVersion", lazy="raise")
    )
    __tablename__ = "revision_secrets"
    __table_args__ = (UniqueConstraint("revision_id", "target", name="uq_revision_secret_target"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    revision_id: UUID = Field(foreign_key="installation_revisions.id", ondelete="RESTRICT")
    version_id: UUID = Field(foreign_key="secret_versions.id", ondelete="RESTRICT", index=True)
    target: str = Field(max_length=512)
    use: str = Field(max_length=64)


class RevisionKubernetesSecret(AuditTrailMixin, table=True):
    """Record a separate namespaced Kubernetes reference without confidential data."""

    __tablename__ = "revision_kubernetes_secrets"
    __table_args__ = (
        UniqueConstraint("revision_id", "target", name="uq_revision_kubernetes_secret_target"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    revision_id: UUID = Field(foreign_key="installation_revisions.id", ondelete="RESTRICT")
    target: str = Field(max_length=512)
    use: str = Field(max_length=64)
    name: str = Field(max_length=253)
    key: str = Field(max_length=253)


for _model, _fields in (
    (ApplicationInstallation, ("id", "name", "owner_id", "cluster_id", "namespace", "revision_id")),
    (InstallationRevision, ("id", "installation_id", "release_id")),
    (ApplicationSecret, ("id", "secret_id", "installation_id")),
    (RevisionSecret, ("id", "revision_id", "version_id", "target", "use")),
    (RevisionKubernetesSecret, ("id", "revision_id", "target", "use", "name", "key")),
):
    register(ModelRegistration(_model, "installations", _model.__name__, _fields))
