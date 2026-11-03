"""Minimal durable work identity, optional target and lifecycle."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field
from tinypods_core.json_types import JsonRecord
from tinypods_core.persistence import CoreModel


class JobStatus(StrEnum):
    """Describe durable application state, independent of RQ retry registries."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(CoreModel, table=True):
    """Persist accepted work without domain-specific columns or queue credentials."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("task ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'", name="task"),
        CheckConstraint(
            "jsonb_typeof(data) = 'object' AND octet_length(data::text) <= 16384", name="data"
        ),
        CheckConstraint("(target_type IS NULL) = (target_id IS NULL)", name="target_pair"),
        CheckConstraint(
            "status IN ('pending','queued','running','succeeded','failed','cancelled')",
            name="status",
        ),
        CheckConstraint("started IS NULL OR started >= created", name="started"),
        CheckConstraint("completed IS NULL OR completed >= created", name="completed"),
        Index("ix_jobs_pending", "status", "id"),
        Index("ix_jobs_target_history", "target_type", "target_id", "created", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    task: str = Field(max_length=128)
    data: JsonRecord = Field(sa_column=Column(JSONB, nullable=False), repr=False)
    target_type: str | None = Field(default=None, max_length=128)
    target_id: UUID | None = None
    requested_by_id: UUID | None = Field(
        default=None, foreign_key="auth_user.id", ondelete="RESTRICT"
    )
    status: str = Field(default=JobStatus.PENDING, max_length=16)
    created: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    scheduled_for: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    started: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    completed: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
