"""Physical object-type registry and immutable generic change history."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field
from tinypods_core.audit.registry import ObjectReference
from tinypods_core.json_types import JsonRecord
from tinypods_core.persistence import CoreModel


class ObjectType(CoreModel, table=True):
    """Historical model label; resolution uses the installed explicit registry only."""

    __tablename__ = "object_types"
    __table_args__ = (UniqueConstraint("app_label", "model"),)
    id: UUID = Field(primary_key=True)
    app_label: str = Field(max_length=64)
    model: str = Field(max_length=128)


class AuditRecord(CoreModel, table=True):
    """One finalized transaction-level change; target deletion preserves this record."""

    __tablename__ = "audit_records"
    __table_args__ = (
        CheckConstraint("action IN ('create', 'update', 'delete')", name="action"),
        CheckConstraint(
            "(related_object_type_id IS NULL) = (related_object_id IS NULL)", name="related_pair"
        ),
        CheckConstraint(
            "(prechange_data IS NULL OR jsonb_typeof(prechange_data) = 'object') "
            "AND (postchange_data IS NULL OR jsonb_typeof(postchange_data) = 'object')",
            name="data_objects",
        ),
        CheckConstraint(
            "(action = 'create' AND prechange_data IS NULL AND postchange_data IS "
            "NOT NULL) OR (action = 'delete' AND prechange_data IS NOT NULL AND "
            "postchange_data IS NULL) OR (action = 'update' AND prechange_data IS "
            "NOT NULL AND postchange_data IS NOT NULL)",
            name="action_data",
        ),
        Index(
            "ix_audit_records_object_time", "changed_object_type_id", "changed_object_id", "time"
        ),
        Index(
            "ix_audit_records_related_time", "related_object_type_id", "related_object_id", "time"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    time: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    user_id: UUID | None = Field(
        default=None, foreign_key="auth_user.id", ondelete="SET NULL", index=True
    )
    user_name: str = Field(max_length=128)
    request_id: UUID = Field(index=True)
    action: str = Field(max_length=6)
    changed_object_type_id: UUID = Field(foreign_key="object_types.id", ondelete="RESTRICT")
    changed_object_id: str = Field(max_length=128)
    related_object_type_id: UUID | None = Field(
        default=None, foreign_key="object_types.id", ondelete="RESTRICT"
    )
    related_object_id: str | None = Field(default=None, max_length=128)
    object_repr: str = Field(max_length=255)
    prechange_data: JsonRecord | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    postchange_data: JsonRecord | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )

    @property
    def changed_object(self) -> ObjectReference:
        """Return an explicit async resolver without hidden database I/O."""
        return ObjectReference(self.changed_object_type_id, self.changed_object_id)

    @property
    def related_object(self) -> ObjectReference | None:
        """Resolve the optional related reference through trusted installed models."""
        if self.related_object_type_id is None or self.related_object_id is None:
            return None
        return ObjectReference(self.related_object_type_id, self.related_object_id)
