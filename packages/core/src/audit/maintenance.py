"""Explicit synchronous operator maintenance; never used by application writes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, Table, and_, delete, func, inspect, select, text, tuple_
from sqlalchemy.sql.elements import ColumnElement
from tinypods_core.audit.models import AuditRecord, ObjectType
from tinypods_core.audit.registry import registry
from tinypods_core.settings import ConfigurationError


@dataclass(frozen=True)
class TypeCleanup:
    """One stale model label and the historical references protecting it."""

    id: UUID
    app_label: str
    model: str
    changed_references: int
    related_references: int
    deleted: bool


def complete_registry() -> frozenset[tuple[str, str]]:
    """Load the intentional core manifest and reject partial/modified registration.

    Returns:
        All explicitly registered installed application/model labels.

    Raises:
        ConfigurationError: The complete installed core registry is unavailable.
    """
    # Import failure aborts maintenance. Model registration is not inferred from
    # route visibility, cached startup state or database target existence.
    from tinypods_core.db.models import CORE_AUDIT_MODELS

    for model, fields, identity in CORE_AUDIT_MODELS:
        spec = registry.get(model)
        if spec is None or (spec.app_label, spec.name, spec.fields, spec.identity) != (
            "auth",
            model.__name__,
            fields,
            identity,
        ):
            raise ConfigurationError("Complete core audit model registration is required")
    return frozenset((spec.app_label, spec.name) for spec in registry.values())


def cleanup_object_types(connection: Connection, *, apply: bool = False) -> list[TypeCleanup]:
    """List stale types; delete only unreferenced rows when explicitly requested.

    Args:
        connection: Operator-owned transaction on the direct migration connection.
        apply: Delete eligible stale types rather than only reporting them.

    Returns:
        Stale labels, reference counts and actual deletion outcomes.
    """
    active = complete_registry()
    types = cast(Table, inspect(ObjectType).local_table)
    records = cast(Table, inspect(AuditRecord).local_table)
    query = (
        select(types)
        .where(tuple_(types.c.app_label, types.c.model).not_in(active))
        .order_by(types.c.app_label, types.c.model)
    )
    if apply:
        # FOR UPDATE fences concurrent FK references; restrictive FKs remain the
        # final race protection. This never cascades into historical records.
        query = query.with_for_update()
    rows = connection.execute(query).mappings().all()
    result = []
    for row in rows:
        changed = connection.execute(
            select(func.count())
            .select_from(records)
            .where(records.c.changed_object_type_id == row["id"])
        ).scalar_one()
        related = connection.execute(
            select(func.count())
            .select_from(records)
            .where(records.c.related_object_type_id == row["id"])
        ).scalar_one()
        removable = not changed and not related
        removed = False
        if apply and removable:
            removed = (
                connection.execute(
                    delete(types).where(types.c.id == row["id"]).returning(types.c.id)
                ).scalar_one_or_none()
                is not None
            )
        result.append(
            TypeCleanup(row["id"], row["app_label"], row["model"], changed, related, removed)
        )
    return result


@dataclass(frozen=True)
class PruneWindow:
    """UTC half-open history interval; absent start means all time before end."""

    start: datetime | None
    end: datetime


def prune_window(
    older_than_days: int | None,
    start: str | None,
    end: str | None,
    *,
    now: datetime,
) -> PruneWindow:
    """Validate mutually exclusive age/range selection before opening a database.

    Args:
        older_than_days: Positive age relative to the command's single UTC clock read.
        start: Inclusive ISO 8601 timestamp with explicit timezone offset or Z.
        end: Exclusive ISO 8601 timestamp with explicit timezone offset or Z.
        now: Execution time, used once to freeze the age cutoff.

    Returns:
        An explicit UTC selection without naive timestamp interpretation.

    Raises:
        ValueError: A mode is missing, conflicting, nonpositive or malformed.
    """
    if older_than_days is not None:
        if start is not None or end is not None:
            raise ValueError("Choose either --older-than-days or --start and --end")
        if older_than_days <= 0:
            raise ValueError("--older-than-days must be positive")
        try:
            cutoff = now.astimezone(UTC) - timedelta(days=older_than_days)
        except OverflowError:
            raise ValueError("--older-than-days exceeds the supported timestamp range") from None
        return PruneWindow(None, cutoff)
    if start is None or end is None:
        raise ValueError("Provide --older-than-days or both --start and --end")
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        raise ValueError("Use ISO 8601 timestamps with a timezone offset or Z") from None
    if first.tzinfo is None or last.tzinfo is None:
        raise ValueError("Timestamps require a timezone offset or Z")
    first, last = first.astimezone(UTC), last.astimezone(UTC)
    if first >= last:
        raise ValueError("--start must be strictly earlier than --end")
    return PruneWindow(first, last)


def prune_records(
    connection: Connection,
    window: PruneWindow,
    *,
    apply: bool = False,
    batch_size: int = 500,
) -> tuple[int, int]:
    """Count or explicitly prune matched history in bounded, atomic operator batches.

    Args:
        connection: Direct operator transaction; role must own the history table.
        window: Validated UTC half-open selection.
        apply: Perform deletion when true; otherwise leave all history untouched.
        batch_size: Positive processing size, never an overall history limit.

    Returns:
        Initially matched count and actual deleted count.

    Raises:
        ValueError: Batch size or timestamp window is invalid.
    """
    if batch_size <= 0:
        raise ValueError("Batch size must be positive")
    if window.end.tzinfo is None or (
        window.start is not None and (window.start.tzinfo is None or window.start >= window.end)
    ):
        raise ValueError("Pruning requires an aware, increasing timestamp window")
    records = cast(Table, inspect(AuditRecord).local_table)
    condition: ColumnElement[bool] = records.c.time < window.end
    if window.start is not None:
        condition = and_(condition, records.c.time >= window.start)
    count = connection.execute(
        select(func.count()).select_from(records).where(condition)
    ).scalar_one()
    if not apply:
        return count, 0
    start = window.start.isoformat() if window.start is not None else "-infinity"
    # A transaction-local, owner-only trigger exception authorizes this exact
    # interval; ordinary deletes and all updates remain append-only violations.
    connection.execute(
        text(
            "SELECT set_config('tinypods.audit_prune_start', :start, true), "
            "set_config('tinypods.audit_prune_end', :end, true)"
        ),
        {"start": start, "end": window.end.isoformat()},
    )
    deleted = 0
    while True:
        candidates = (
            select(records.c.id)
            .where(condition)
            .order_by(records.c.time, records.c.id)
            .limit(batch_size)
            .with_for_update()
        )
        removed = connection.execute(
            delete(records).where(condition, records.c.id.in_(candidates)).returning(records.c.id)
        ).all()
        deleted += len(removed)
        if len(removed) < batch_size:
            break
    connection.execute(
        text(
            "SELECT set_config('tinypods.audit_prune_start', '', true), "
            "set_config('tinypods.audit_prune_end', '', true)"
        )
    )
    return count, deleted
