"""Explicit audit maintenance uses isolated fixtures and never operator data."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_audit_trail import audit_settings as audit_settings
from tinypods_cli.main import app
from tinypods_core.audit import AuditRecord, ObjectType
from tinypods_core.audit.maintenance import (
    PruneWindow,
    cleanup_object_types,
    complete_registry,
    prune_records,
    prune_window,
)
from tinypods_core.audit.registry import registry
from tinypods_core.db import migration_engine
from tinypods_core.db.models import User
from tinypods_core.settings import ConfigurationError, Settings
from typer.testing import CliRunner


def invoke(settings: Settings, *args: str) -> dict[str, object]:
    """Invoke a real command using only the disposable fixture URL.

    Args:
        settings: Isolated migrated PostgreSQL.
        args: Explicit CLI arguments.

    Returns:
        Parsed structured command result.
    """
    result = CliRunner().invoke(
        app,
        ["audit", *args],
        env={
            "TINYPODS_DATABASE_URL": settings.database_url.get_secret_value(),
            "TINYPODS_MIGRATION_DATABASE_URL": settings.database_url.get_secret_value(),
            "TINYPODS_DATABASE_MODE": "direct",
        },
    )
    assert result.exit_code == 0, result.output + str(result.exception)
    return json.loads(result.stdout)


def seed_history(settings: Settings, times: list[datetime]) -> list[object]:
    """Insert explicit historical fixture rows without changing existing timestamps.

    Args:
        settings: Disposable migrated database.
        times: Exact UTC timestamps whose boundary behavior will be tested.

    Returns:
        Inserted event identities.
    """
    table = inspect(AuditRecord).local_table
    rows = [
        AuditRecord(
            time=when,
            user_name="system:test",
            request_id=uuid4(),
            action="create",
            changed_object_type_id=registry[User].type_id,
            changed_object_id=str(uuid4()),
            object_repr="safe test record",
            postchange_data={"safe": True},
        ).model_dump()
        for when in times
    ]
    engine = migration_engine(settings)
    try:
        with engine.begin() as connection:
            connection.execute(table.insert(), rows)
        return [row["id"] for row in rows]
    finally:
        engine.dispose()


def test_cleanup_preserves_active_and_referenced_types(audit_settings: Settings) -> None:
    """Dry runs are read-only and both historical reference roles prevent deletion.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    engine = migration_engine(audit_settings)
    types = inspect(ObjectType).local_table
    records = inspect(AuditRecord).local_table
    free, changed, related = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                types.insert(),
                [
                    dict(id=identity, app_label="removed", model=name)
                    for identity, name in [
                        (free, "Free"),
                        (changed, "Changed"),
                        (related, "Related"),
                    ]
                ],
            )
            record = AuditRecord(
                user_name="system:test",
                request_id=uuid4(),
                action="create",
                changed_object_type_id=changed,
                changed_object_id="historical",
                related_object_type_id=related,
                related_object_id="historical-related",
                object_repr="historical",
                postchange_data={"safe": True},
            )
            connection.execute(records.insert(), record.model_dump())
        dry = invoke(audit_settings, "cleanup-types")
        assert dry["deleted"] == 0 and len(dry["stale"]) == 3
        assert dry["retained"] == 2
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(types)) == 11
        applied = invoke(audit_settings, "cleanup-types", "--apply")
        assert applied["deleted"] == 1 and applied["retained"] == 2
        again = invoke(audit_settings, "cleanup-types", "--apply")
        assert again["deleted"] == 0 and again["retained"] == 2
        with engine.connect() as connection:
            assert set(connection.execute(select(types.c.id)).scalars()) == {
                spec.type_id for spec in registry.values()
            } | {
                changed,
                related,
            }
            assert connection.scalar(select(func.count()).select_from(records)) == 1
    finally:
        engine.dispose()


def test_cleanup_rejects_incomplete_registry(audit_settings: Settings) -> None:
    """A missing or altered installed registration cannot make an active type stale.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    from dataclasses import replace

    original = registry.pop(User)
    engine = migration_engine(audit_settings)
    try:
        with engine.begin() as connection, pytest.raises(ConfigurationError, match="Complete core"):
            cleanup_object_types(connection, apply=True)
        registry[User] = replace(original, fields=("id",))
        with pytest.raises(ConfigurationError, match="Complete core"):
            complete_registry()
    finally:
        registry[User] = original
        engine.dispose()


def test_prune_age_and_range_boundaries(audit_settings: Settings) -> None:
    """Pruning is dry-run by default, half-open, bounded and idempotent.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    now = datetime(2026, 11, 1, tzinfo=UTC)
    cutoff = now - timedelta(days=90)
    window = prune_window(90, None, None, now=now)
    assert window == PruneWindow(None, cutoff)
    ids = seed_history(
        audit_settings,
        [cutoff - timedelta(microseconds=1), cutoff, cutoff + timedelta(microseconds=1), now],
    )
    engine = migration_engine(audit_settings)
    try:
        with engine.begin() as connection:
            assert prune_records(connection, window) == (1, 0)
        with engine.begin() as connection:
            assert prune_records(connection, window, apply=True, batch_size=1) == (1, 1)
            # Scope is cleared after maintenance; a normal delete remains forbidden.
            with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                connection.execute(text("DELETE FROM audit_records"))
        with engine.begin() as connection:
            assert prune_records(connection, window, apply=True) == (0, 0)
            remaining = list(
                connection.execute(select(inspect(AuditRecord).local_table.c.id)).scalars()
            )
            assert set(remaining) == set(ids[1:])
        # Explicit offsets normalize to UTC and include start while excluding end.
        first, last = cutoff, now
        dry = invoke(
            audit_settings, "prune", "--start", first.isoformat(), "--end", last.isoformat()
        )
        assert dry["matched"] == 2 and dry["deleted"] == 0
        applied = invoke(
            audit_settings,
            "prune",
            "--start",
            first.isoformat(),
            "--end",
            last.isoformat(),
            "--apply",
            "--batch-size",
            "1",
        )
        assert applied["deleted"] == 2
        assert (
            invoke(
                audit_settings,
                "prune",
                "--start",
                first.isoformat(),
                "--end",
                last.isoformat(),
                "--apply",
            )["deleted"]
            == 0
        )
        with engine.connect() as connection:
            assert list(
                connection.execute(select(inspect(AuditRecord).local_table.c.id)).scalars()
            ) == [ids[-1]]
            assert (
                connection.scalar(select(func.count()).select_from(inspect(ObjectType).local_table))
                == 8
            )
        assert invoke(audit_settings, "prune", "--older-than-days", "1")["apply"] is False
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--older-than-days", "0"],
        ["--older-than-days", "-1"],
        ["--start", "2026-01-01T00:00:00Z"],
        ["--end", "2026-01-01T00:00:00Z"],
        [
            "--older-than-days",
            "90",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-02-01T00:00:00Z",
        ],
        ["--start", "bad", "--end", "bad"],
        ["--start", "2026-01-01", "--end", "2026-02-01"],
        ["--start", "2026-02-01T00:00:00Z", "--end", "2026-01-01T00:00:00Z"],
        ["--start", "2026-01-01T00:00:00Z", "--end", "2026-01-01T00:00:00Z"],
        ["--older-than-days", "999999999999999999"],
        ["--older-than-days", "90", "--batch-size", "0"],
    ],
)
def test_prune_rejects_invalid_modes_without_database(args: list[str]) -> None:
    """Invalid selection never opens a database or silently interprets local time.

    Args:
        args: Invalid CLI combination or timestamp selection.
    """
    result = CliRunner().invoke(app, ["audit", "prune", *args])
    assert result.exit_code == 2


def test_prune_validates_windows_and_rolls_back_batches(audit_settings: Settings) -> None:
    """Invalid windows and a failed batch leave all selected history intact.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    now = datetime.now(UTC)
    assert prune_window(
        None, "2026-01-01T05:30:00+05:30", "2026-01-02T00:00:00Z", now=now
    ).start == datetime(2026, 1, 1, tzinfo=UTC)
    ids = seed_history(audit_settings, [now - timedelta(days=100)] * 3)
    engine = migration_engine(audit_settings)
    try:
        with engine.begin() as connection:
            for window, batch in [
                (PruneWindow(None, now), 0),
                (PruneWindow(None, now.replace(tzinfo=None)), 1),
                (PruneWindow(now, now), 1),
                (PruneWindow(now.replace(tzinfo=None), now), 1),
            ]:
                with pytest.raises(ValueError):
                    prune_records(connection, window, apply=True, batch_size=batch)
        with pytest.raises(RuntimeError, match="abort"), engine.begin() as connection:
            assert prune_records(connection, PruneWindow(None, now), apply=True, batch_size=1) == (
                3,
                3,
            )
            raise RuntimeError("abort all deletion batches")
        with engine.connect() as connection:
            assert set(
                connection.execute(select(inspect(AuditRecord).local_table.c.id)).scalars()
            ) == set(ids)
    finally:
        engine.dispose()


def test_cleanup_foreign_key_race_cannot_orphan_history(audit_settings: Settings) -> None:
    """A concurrent writer cannot reference a stale type being removed.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    types = inspect(ObjectType).local_table
    records = inspect(AuditRecord).local_table
    identity = uuid4()
    engine = migration_engine(audit_settings)
    try:
        with engine.begin() as connection:
            connection.execute(
                types.insert().values(id=identity, app_label="removed", model="Racing")
            )
        with engine.begin() as cleanup:
            result = cleanup_object_types(cleanup, apply=True)
            assert result[0].deleted
            with engine.begin() as writer, pytest.raises(DBAPIError):
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                record = AuditRecord(
                    user_name="system:old-process",
                    request_id=uuid4(),
                    action="create",
                    changed_object_type_id=identity,
                    changed_object_id="old-target",
                    object_repr="safe",
                    postchange_data={},
                )
                writer.execute(records.insert(), record.model_dump())
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(records)) == 0
            assert (
                connection.scalar(
                    select(func.count()).select_from(types).where(types.c.id == identity)
                )
                == 0
            )
    finally:
        engine.dispose()


def test_prune_permission_and_exact_window_guard(audit_settings: Settings) -> None:
    """Only the owner maintenance path may bypass deletion for its exact time window.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    now = datetime.now(UTC)
    start, end = now - timedelta(days=2), now - timedelta(days=1)
    ids = seed_history(audit_settings, [start - timedelta(seconds=1), start, end])
    engine = migration_engine(audit_settings)
    role = "audit_prune_test_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT set_config('tinypods.audit_prune_start', :start, true), "
                    "set_config('tinypods.audit_prune_end', :end, true)"
                ),
                {"start": start.isoformat(), "end": end.isoformat()},
            )
            for identity in [ids[0], ids[-1]]:
                with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                    connection.execute(
                        text("DELETE FROM audit_records WHERE id=:id"), {"id": identity}
                    )
            with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                connection.execute(
                    text("UPDATE audit_records SET user_name='tampered' WHERE id=:id"),
                    {"id": ids[1]},
                )
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE ROLE {role}")
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            connection.exec_driver_sql(f"GRANT SELECT, DELETE, UPDATE ON audit_records TO {role}")
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.exec_driver_sql(f"SET LOCAL ROLE {role}")
            prune_records(connection, PruneWindow(start, end), apply=True)
        with engine.begin() as connection:
            assert prune_records(connection, PruneWindow(start, end), apply=True) == (1, 1)
        with engine.connect() as connection:
            assert set(
                connection.execute(select(inspect(AuditRecord).local_table.c.id)).scalars()
            ) == {ids[0], ids[-1]}
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"REVOKE ALL ON audit_records FROM {role}")
            connection.exec_driver_sql(f"REVOKE USAGE ON SCHEMA public FROM {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")
        engine.dispose()


def test_cleanup_preserves_registered_labels_without_targets(audit_settings: Settings) -> None:
    """Staleness follows installed model labels, not target rows or an incidental PK.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    engine = migration_engine(audit_settings)
    table = inspect(ObjectType).local_table
    alternate_id = uuid4()
    try:
        with engine.begin() as connection:
            assert (
                connection.scalar(select(func.count()).select_from(inspect(User).local_table)) == 0
            )
            connection.execute(
                table.update().where(table.c.id == registry[User].type_id).values(id=alternate_id)
            )
        assert invoke(audit_settings, "cleanup-types", "--apply")["deleted"] == 0
        with engine.connect() as connection:
            assert (
                connection.scalar(select(table.c.id).where(table.c.model == "User")) == alternate_id
            )
    finally:
        engine.dispose()
