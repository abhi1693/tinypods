"""Shared versioned secret metadata with enforced, real cluster scope."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    UniqueConstraint,
)
from sqlalchemy.orm import deferred, relationship
from sqlmodel import Field, Relationship
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.audit.registry import ModelRegistration, register

if TYPE_CHECKING:
    from tinypods_core.clusters import Cluster


class Secret(AuditTrailMixin, table=True):
    """Store safe metadata only; every secret must have its matching cluster scope."""

    __tablename__ = "secrets"
    __table_args__ = (CheckConstraint("kind = 'cluster'", name="kind"),)
    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "name", "description", "kind")
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    kind: str = Field(default="cluster", max_length=32)


_nonce: Column[bytes] = Column("nonce", LargeBinary, nullable=False)
_payload: Column[bytes] = Column("encrypted_payload", LargeBinary, nullable=False)


class SecretVersion(AuditTrailMixin, table=True):
    """Keep immutable logical versions; encryption-key rotation preserves their identities."""

    __tablename__ = "secret_versions"
    __table_args__ = (
        UniqueConstraint("secret_id", "version", name="uq_secret_versions_number"),
        UniqueConstraint("id", "secret_id", name="uq_secret_versions_identity_scope"),
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("octet_length(nonce) = 12", name="nonce"),
        CheckConstraint("octet_length(encrypted_payload) >= 16", name="payload"),
    )
    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "version", "revoked_at")
    secret: Secret = Relationship(sa_relationship=relationship("Secret", lazy="raise"))
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    secret_id: UUID = Field(foreign_key="secrets.id", ondelete="RESTRICT", index=True)
    version: int
    encryption_key_id: str = Field(max_length=64, index=True)
    nonce: bytes = Field(repr=False, sa_column=_nonce)
    encrypted_payload: bytes = Field(repr=False, sa_column=_payload)
    __mapper_args__ = {
        "properties": {"nonce": deferred(_nonce), "encrypted_payload": deferred(_payload)}
    }
    revoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class ClusterSecret(AuditTrailMixin, table=True):
    """Associate one kubeconfig secret with a cluster and pin its active version."""

    __tablename__ = "cluster_secrets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["active_version_id", "secret_id"],
            ["secret_versions.id", "secret_versions.secret_id"],
            name="fk_cluster_secrets_active_scope",
            ondelete="RESTRICT",
        ),
    )
    brief_fields: ClassVar[tuple[str, ...] | None] = ("id", "secret", "cluster")
    secret: Secret = Relationship(
        sa_relationship=relationship(
            "Secret", lazy="raise", foreign_keys="[ClusterSecret.secret_id]"
        )
    )
    cluster: Cluster = Relationship(sa_relationship=relationship("Cluster", lazy="raise"))
    active_version: SecretVersion | None = Relationship(
        sa_relationship=relationship(
            "SecretVersion",
            lazy="raise",
            viewonly=True,
            foreign_keys="[ClusterSecret.active_version_id, ClusterSecret.secret_id]",
        )
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    secret_id: UUID = Field(foreign_key="secrets.id", ondelete="CASCADE", unique=True)
    cluster_id: UUID = Field(foreign_key="clusters.id", ondelete="RESTRICT", unique=True)
    active_version_id: UUID | None = None


for model, fields in (
    (Secret, ("id", "name", "description", "kind")),
    (SecretVersion, ("id", "secret_id", "version", "encryption_key_id", "revoked_at")),
    (ClusterSecret, ("id", "secret_id", "cluster_id", "active_version_id")),
):
    register(ModelRegistration(model, "secrets", model.__name__, fields))
