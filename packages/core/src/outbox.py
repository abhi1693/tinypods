"""Reusable transactional event persistence without transport dependencies."""

import json
import re
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import CheckConstraint, Column, DateTime, Index, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.json_types import JsonRecord
from tinypods_core.persistence import CoreModel

MAX_PAYLOAD_BYTES = 16_384
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class OutboxEvent(CoreModel, table=True):
    """Persist immutable event content independently of its producing domain."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("event_type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'", name="event_type"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint("octet_length(payload::text) <= 16384", name="payload_size"),
        CheckConstraint("published_at IS NULL OR published_at >= created", name="publication_time"),
        Index("ix_outbox_pending", "created", "id", postgresql_where=text("published_at IS NULL")),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field(max_length=128)
    payload: JsonRecord = Field(sa_column=Column(JSONB, nullable=False), repr=False)
    created: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class EventPayload(BaseModel):
    """Base for code-owned schemas containing only safe delivery metadata.

    Subclasses fix event_type as a class variable identifying their payload contract.
    Producers own authorization and field allowlists; this boundary cannot identify secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    event_type: ClassVar[str]


def append_event(session: AsyncSession, payload: EventPayload) -> OutboxEvent:
    """Snapshot a typed payload in the caller's business transaction.

    No I/O, flush, commit, rollback or session ownership is taken here.

    Args:
        session: Session owning the related business changes.
        payload: Validated domain instance with code-owned type.

    Returns:
        Unsaved event with stable identity for later duplicate detection.

    Raises:
        ValueError: The event contract or serialized payload is invalid or oversized.
    """
    if not re.fullmatch(EVENT_TYPE_PATTERN, payload.event_type) or len(payload.event_type) > 128:
        raise ValueError("Invalid outbox event contract")
    # Revalidate model_construct/model_copy inputs before crossing persistence.
    validated = type(payload).model_validate(payload.model_dump(mode="python", warnings=False))
    encoded = json.dumps(validated.model_dump(mode="json"), allow_nan=False, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("Outbox payload exceeds 16384 bytes")
    record = OutboxEvent(
        event_type=payload.event_type,
        payload=json.loads(encoded),
    )
    session.add(record)
    return record


async def pending_events(session: AsyncSession, *, limit: int = 100) -> list[OutboxEvent]:
    """Read a bounded oldest-first batch without claiming or publishing events.

    Args:
        session: Caller-owned short read session.
        limit: Batch size between one and one thousand.

    Returns:
        Pending events ordered deterministically by creation time and identity.

    Raises:
        ValueError: The batch size is outside the supported bound.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("Outbox batch limit must be between 1 and 1000")
    return list(
        (
            await session.exec(
                select(OutboxEvent)
                .where(col(OutboxEvent.published_at).is_(None))
                .order_by(col(OutboxEvent.created), col(OutboxEvent.id))
                .limit(limit)
            )
        ).all()
    )


async def acknowledge_event(session: AsyncSession, event_id: UUID, published_at: datetime) -> bool:
    """Record publication acknowledgment in the caller's short transaction.

    Repeated or unknown identities do not rewrite the first acknowledgment.
    Publication is separate from consumer completion.

    Args:
        session: Caller-owned acknowledgment transaction.
        event_id: Stable identity supplied to the eventual transport.
        published_at: Timezone-aware publication acknowledgment time.

    Returns:
        Whether this call marked the previously pending event.

    Raises:
        ValueError: The supplied time has no timezone.
    """
    if published_at.utcoffset() is None:
        raise ValueError("Publication time must be timezone-aware")
    result = await session.exec(
        update(OutboxEvent)
        .where(col(OutboxEvent.id) == event_id, col(OutboxEvent.published_at).is_(None))
        .values(published_at=published_at)
        .returning(col(OutboxEvent.id))
    )
    return result.scalar_one_or_none() is not None
