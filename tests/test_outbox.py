"""Real PostgreSQL atomicity and transport-independent outbox invariants."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from test_auth import auth_settings as auth_settings
from tinypods_core.db import Database, MigrationManager
from tinypods_core.db.models import Installation, User
from tinypods_core.outbox import (
    EventPayload,
    OutboxEvent,
    acknowledge_event,
    append_event,
    pending_events,
)
from tinypods_core.settings import Settings


class ReleasePublished(EventPayload):
    """Synthetic catalog schema; does not change production publication behavior."""

    event_type: ClassVar[str] = "catalog.release.published"
    release_id: UUID


class MemberCreated(EventPayload):
    """Independent domain schema using the same persistence contract."""

    event_type: ClassVar[str] = "identity.member.created"
    member_id: UUID
    metadata: dict[str, str] = {}


def test_atomic_business_changes_and_reuse(auth_settings: Settings) -> None:
    """Persist domain data and events together; exceptions and cancellation lose both."""

    async def scenario() -> None:
        async with Database(auth_settings) as database:
            async with database.transaction() as session:
                user = User(username="outbox-owner")
                session.add(user)
                event = append_event(session, MemberCreated(member_id=user.id))
                second = append_event(session, ReleasePublished(release_id=uuid4()))
                assert event not in await pending_events(session)
                assert event in session.new
                assert "payload=" not in repr(event)
            async with database.session() as session:
                assert {e.id for e in await pending_events(session)} == {event.id, second.id}
                assert (await session.exec(select(User))).one().id == user.id
                assert (
                    await session.exec(text("SELECT count(*) FROM audit_records"))
                ).scalar() == 1
            original = (await database.installation()).installation_id
            for cancel in (False, True):
                ready = asyncio.Event()

                async def failed(ready: asyncio.Event, cancel: bool) -> None:
                    async with database.transaction() as session:
                        business = await session.get(Installation, 1)
                        assert business is not None
                        business.installation_id = uuid4()
                        append_event(session, ReleasePublished(release_id=uuid4()))
                        await session.flush()
                        ready.set()
                        if cancel:
                            await asyncio.Event().wait()
                        raise RuntimeError("abort")

                task = asyncio.create_task(failed(ready, cancel))
                async with asyncio.timeout(5):
                    await ready.wait()
                    if cancel:
                        task.cancel()
                    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                        await task
                assert (await database.installation()).installation_id == original
                async with database.session() as session:
                    assert len(await pending_events(session)) == 2
            # A later business constraint failure also rolls back the flushed event.
            with pytest.raises(IntegrityError, match="duplicate key"):
                async with database.transaction() as session:
                    append_event(session, ReleasePublished(release_id=uuid4()))
                    await session.flush()
                    session.add(User(username="outbox-owner"))
            async with database.session() as session:
                assert len(await pending_events(session)) == 2

    asyncio.run(scenario())


def test_payload_validation_and_snapshot(auth_settings: Settings) -> None:
    """Reject invalid contracts, extra fields, nonfinite JSON and oversized payloads."""

    async def scenario() -> None:
        async with Database(auth_settings) as database, database.transaction() as session:
            metadata = {"safe": "value"}
            payload = MemberCreated(member_id=uuid4(), metadata=metadata)
            event = append_event(session, payload)
            payload.metadata["safe"] = "changed"
            assert event.payload["metadata"] == {"safe": "value"}
            with pytest.raises(ValidationError):
                MemberCreated(member_id=uuid4(), extra="private")
            with pytest.raises(ValidationError):
                append_event(
                    session, ReleasePublished.model_construct(release_id="invalid-private")
                )
            for name in ("invalid", "a." + "b" * 127):

                class Invalid(EventPayload):
                    event_type: ClassVar[str] = name

                with pytest.raises(ValueError, match="contract"):
                    append_event(session, Invalid())

            class Numeric(EventPayload):
                event_type: ClassVar[str] = "test.numeric"
                value: float

            for value in (float("nan"), float("inf"), -float("inf")):
                with pytest.raises(ValueError):
                    append_event(session, Numeric(value=value))
            with pytest.raises(ValueError, match="16384"):
                append_event(
                    session, MemberCreated(member_id=uuid4(), metadata={"large": "x" * 16384})
                )
            with pytest.raises(ValueError, match="16384"):
                append_event(
                    session, MemberCreated(member_id=uuid4(), metadata={"large": "界" * 6000})
                )
            for limit in (0, 1001):
                with pytest.raises(ValueError, match="limit"):
                    await pending_events(session, limit=limit)
            with pytest.raises(ValueError, match="timezone"):
                await acknowledge_event(session, event.id, datetime(2026, 1, 1))

    asyncio.run(scenario())


def test_order_acknowledgment_and_pending_index(auth_settings: Settings) -> None:
    """Read bounded deterministic pending batches and acknowledge each identity once."""

    async def scenario() -> None:
        async with Database(auth_settings) as database:
            created = datetime.now(UTC)
            async with database.transaction() as session:
                events = [
                    append_event(session, ReleasePublished(release_id=uuid4())) for _ in range(4)
                ]
                for event in events:
                    event.created = created
                expected = sorted(events, key=lambda event: event.id)
            async with database.session() as session:
                assert [e.id for e in await pending_events(session, limit=2)] == [
                    e.id for e in expected[:2]
                ]
                await session.exec(text("SET LOCAL enable_seqscan = off"))
                plan = (
                    await session.exec(
                        text(
                            "EXPLAIN SELECT * FROM outbox_events WHERE published_at IS NULL "
                            "ORDER BY created, id LIMIT 100"
                        )
                    )
                ).all()
                assert "ix_outbox_pending" in str(plan)
            published = created + timedelta(seconds=1)
            async with database.transaction() as session:
                assert await acknowledge_event(session, expected[0].id, published)
                assert not await acknowledge_event(
                    session, expected[0].id, published + timedelta(seconds=1)
                )
                assert not await acknowledge_event(session, uuid4(), published)
            async with database.session() as session:
                assert [e.id for e in await pending_events(session)] == [e.id for e in expected[1:]]
                record = await session.get(OutboxEvent, expected[0].id)
                assert record is not None and record.published_at == published

    asyncio.run(scenario())


def test_database_guards_and_migration(auth_settings: Settings) -> None:
    """Direct SQL cannot bypass payload bounds, immutable content or pending retention."""
    url = auth_settings.database_url.get_secret_value()
    event_id = uuid4()
    with psycopg.connect(url) as connection:
        connection.execute(
            "INSERT INTO outbox_events VALUES (%s,'test.event','{}',now(),NULL)", (event_id,)
        )
    statements = [
        ("UPDATE outbox_events SET id=%s", (uuid4(),)),
        ("UPDATE outbox_events SET event_type='changed.event'", ()),
        ("UPDATE outbox_events SET payload='{\"changed\":true}'", ()),
        ("UPDATE outbox_events SET created=created-interval '1 second'", ()),
        ("DELETE FROM outbox_events", ()),
        ("UPDATE outbox_events SET published_at=created-interval '1 second'", ()),
    ]
    for query, params in statements:
        with psycopg.connect(url) as connection, pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(query, params)
    for event_type, payload in [
        ("invalid", "{}"),
        ("a." + "b" * 127, "{}"),
        ("test.event", "[]"),
        ("test.event", "null"),
        ("test.event", '{"data":"' + "x" * 16384 + '"}'),
    ]:
        with psycopg.connect(url) as connection, pytest.raises(psycopg.Error):
            connection.execute(
                "INSERT INTO outbox_events VALUES (%s,%s,%s,now(),NULL)",
                (uuid4(), event_type, payload),
            )
    with psycopg.connect(url) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "INSERT INTO outbox_events VALUES (%s,'test.event','{}',now(),now())", (uuid4(),)
        )
    # The authoritative bound is PostgreSQL's JSONB text, exactly including spaces.
    with psycopg.connect(url) as connection:
        boundary = json.dumps({"data": "x" * (16384 - len('{"data": ""}'))})
        connection.execute(
            "INSERT INTO outbox_events VALUES (%s,'test.event',%s,now(),NULL)",
            (uuid4(), boundary),
        )
    with psycopg.connect(url) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "INSERT INTO outbox_events VALUES (%s,'test.event',%s,now(),NULL)",
            (uuid4(), json.dumps({"data": "x" * (16385 - len('{"data": ""}'))})),
        )
    manager = MigrationManager(auth_settings, Path(__file__).resolve().parents[1] / "alembic.ini")
    manager.check()
    with pytest.raises(RuntimeError, match="history"):
        command.downgrade(manager.config(), "0009_application_installations")
    with psycopg.connect(url) as connection:
        connection.execute("UPDATE outbox_events SET published_at=created")
        connection.execute("UPDATE outbox_events SET published_at=published_at")
    for assignment in ("NULL", "published_at+interval '1 second'"):
        with psycopg.connect(url) as connection, pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE outbox_events SET published_at=" + assignment)
    with psycopg.connect(url) as connection:
        connection.execute("DELETE FROM outbox_events")
    command.downgrade(manager.config(), "0009_application_installations")
    manager.upgrade()
    manager.check()


def test_outbox_failure_rolls_back_business_change(auth_settings: Settings) -> None:
    """A final PostgreSQL payload rejection cannot leave related business data committed."""

    class Measurement(EventPayload):
        event_type: ClassVar[str] = "test.measurement"
        values: list[float]

    async def scenario() -> None:
        async with Database(auth_settings) as database:
            original = (await database.installation()).installation_id
            with pytest.raises(IntegrityError, match="payload_size"):
                async with database.transaction() as session:
                    business = await session.get(Installation, 1)
                    assert business is not None
                    business.installation_id = uuid4()
                    # JSONB expands exponent notation; its stored size is authoritative.
                    append_event(session, Measurement(values=[1e308] * 60))
            assert (await database.installation()).installation_id == original
            async with database.session() as session:
                assert await pending_events(session) == []

    asyncio.run(scenario())
