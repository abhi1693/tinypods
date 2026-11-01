"""Bounded audit batches staged and consolidated inside the business transaction.

PostgreSQL temporary staging preserves complete history without an object-count
ceiling. Savepoints/rollback apply to staged snapshots just like business rows.
"""

from datetime import UTC, datetime
from itertools import chain
from typing import cast
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    MetaData,
    Table,
    and_,
    bindparam,
    case,
    event,
    inspect,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction
from sqlalchemy.orm.unitofwork import UOWTransaction
from sqlalchemy.sql import ClauseElement, visitors
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.selectable import Alias
from sqlmodel import SQLModel
from tinypods_core.audit.context import AuditContext, current_context
from tinypods_core.audit.mixin import AuditAction
from tinypods_core.audit.models import AuditRecord, ObjectType
from tinypods_core.audit.registry import ModelRegistration, registry, safe_value
from tinypods_core.json_types import JsonRecord, JsonValue

BATCH_SIZE = 100


def make_record(
    spec: ModelRegistration,
    obj: SQLModel,
    action: AuditAction,
    before: JsonRecord | None,
    after: JsonRecord | None,
    context: AuditContext | None = None,
) -> AuditRecord:
    """Build safe history without inspecting repr or arbitrary object attributes.

    Args:
        spec: Reviewed registration for this object.
        obj: Changed mapped object.
        action: Lifecycle action.
        before: Detached allowlisted original state.
        after: Detached allowlisted final state.
        context: Transaction-owned fallback attribution when no explicit context exists.

    Returns:
        Unsaved record attributed to the current trusted execution context.
    """
    context = context or current_context.get() or AuditContext(uuid4(), "system:unattributed")
    identity = str(getattr(obj, spec.identity))
    related_type = related_id = None
    if spec.related is not None:
        field, model = spec.related
        value = getattr(obj, field)
        if value is not None:
            related_type, related_id = registry[model].type_id, str(value)
    return AuditRecord(
        related_object_type_id=related_type,
        related_object_id=related_id,
        user_id=context.user_id,
        user_name=context.user_name,
        request_id=context.request_id,
        action=action,
        changed_object_type_id=spec.type_id,
        changed_object_id=identity,
        object_repr=f"{spec.app_label}.{spec.name}:{identity}"[:255],
        prechange_data=before,
        postchange_data=after,
    )


def staging_table(session: Session) -> Table:
    """Create transaction-scoped temporary storage, including after savepoint rollback.

    Args:
        session: Business transaction owner; no separate connection or commit.

    Returns:
        PostgreSQL temporary staging table on this transaction's connection.
    """
    table = session.info.get("audit_staging")
    if table is None:
        source = inspect(AuditRecord).local_table
        table = Table(
            "audit_stage_" + uuid4().hex,
            MetaData(),
            *(
                Column(
                    column.name,
                    column.type,
                    nullable=column.nullable,
                    primary_key=column.name in {"changed_object_type_id", "changed_object_id"},
                )
                for column in source.columns
            ),
            Column("sensitive", Boolean(), nullable=False),
            prefixes=["TEMPORARY"],
            postgresql_on_commit="DROP",
        )
        session.info["audit_staging"] = table
    # Creation can itself have been rolled back to a savepoint. Check on each
    # flush/commit rather than caching a Python flag that outlives that rollback.
    table.create(session.connection(), checkfirst=True)
    return cast(Table, table)


def stage(session: Session, table: Table, rows: list[dict[str, object]]) -> None:
    """Upsert one bounded batch, preserving first-before and latest-after snapshots.

    Args:
        session: Business transaction owner.
        table: Temporary storage already checked once for this flush.
        rows: At most BATCH_SIZE safe detached snapshots.
    """
    statement = insert(table)
    old, new = table.c, statement.excluded
    action = case(
        (new.postchange_data.is_(None), "delete"),
        (old.prechange_data.is_(None), "create"),
        else_="update",
    )
    session.connection().execute(
        statement.on_conflict_do_update(
            index_elements=["changed_object_type_id", "changed_object_id"],
            set_={
                "action": action,
                "postchange_data": new.postchange_data,
                "sensitive": old.sensitive | new.sensitive,
            },
        ),
        rows,
    )


type Candidate = tuple[ModelRegistration, SQLModel, bool, bool, set[str]]


def read_snapshots(
    session: Session, objects: list[tuple[ModelRegistration, SQLModel]], *, lock: bool
) -> dict[tuple[type[SQLModel], str], JsonRecord]:
    """Read only approved persisted fields, grouped by model in bounded batches.

    Args:
        session: Business transaction owner.
        objects: At most BATCH_SIZE mapped objects with stable unique audit IDs.
        lock: Lock original rows before writes when true.

    Returns:
        Safe snapshots keyed by model and identity; absent rows fail capture.
    """
    groups: dict[ModelRegistration, list[SQLModel]] = {}
    for spec, obj in objects:
        groups.setdefault(spec, []).append(obj)
    snapshots = {}
    for spec, members in groups.items():
        mapper = inspect(spec.model)
        assert mapper is not None
        identity = mapper.columns[spec.identity]
        query = (
            select(
                identity.label("_audit_identity"), *(mapper.columns[field] for field in spec.fields)
            )
            .where(identity.in_([getattr(obj, spec.identity) for obj in members]))
            .order_by(identity)
        )
        if lock:
            query = query.with_for_update()
        for row in session.connection().execute(query).mappings():
            snapshots[(spec.model, str(row["_audit_identity"]))] = {
                field: safe_value(row[field]) for field in spec.fields
            }
    return snapshots


def capture_batch(session: Session, batch: list[Candidate], table: Table | None) -> Table:
    """Stage one bounded batch while retaining only object references for post-flush.

    Args:
        session: Business transaction owner.
        batch: Candidate records with lifecycle and dirty-field metadata.
        table: Existing staging table for this flush, or None on its first batch.

    Returns:
        The staging table, checked/created once for this flush.
    """
    originals = read_snapshots(
        session, [(spec, obj) for spec, obj, new, _, _ in batch if not new], lock=True
    )
    rows = []
    for spec, obj, new, deleted, modified in batch:
        before = None
        if not new:
            before = originals[(spec.model, str(getattr(obj, spec.identity)))]
            current = spec.serialize(obj)
            for name in spec.relationships:
                state = inspect(obj)
                assert state is not None
                history = state.attrs[name].history
                members = list(history.unchanged) + list(history.deleted)
                before[name] = list[JsonValue](
                    sorted(
                        str(getattr(member, registry[type(member)].identity)) for member in members
                    )
                )
                if not history.has_changes():
                    before[name] = current[name]
        action: AuditAction = "create" if new else ("delete" if deleted else "update")
        if not deleted:
            obj.last_updated = datetime.now(UTC)
            session.info["audit_refresh"].append((spec, obj))
        fallback = session.info.setdefault(
            "audit_context", AuditContext(uuid4(), "system:unattributed")
        )
        record = make_record(
            spec,
            obj,
            action,
            before,
            None if deleted else spec.serialize(obj),
            current_context.get() or fallback,
        )
        row: dict[str, object] = dict(record.model_dump())
        row["sensitive"] = bool(
            modified - set(spec.fields) - set(spec.relationships) - {spec.identity}
        )
        rows.append(row)
    if table is None:
        table = staging_table(session)
    stage(session, table, rows)
    return table


@event.listens_for(Session, "before_flush")
def capture(session: Session, flush_context: UOWTransaction, instances: object) -> None:
    """Validate unit-of-work writes and stage snapshots with bounded SQL batches.

    Args:
        session: Flushing unit of work.
        flush_context: SQLAlchemy flush lifecycle.
        instances: Optional partial flush, unsupported for audited writes.
    """
    batch: list[Candidate] = []
    table = None
    session.info["audit_refresh"] = []
    for obj in chain(session.new, session.dirty, session.deleted):
        if isinstance(obj, (AuditRecord, ObjectType)):
            raise ValueError("Change history and object types require controlled writes")
        spec = registry.get(type(obj))
        if spec is None:
            continue
        if instances is not None:
            raise ValueError("Partial flush is unsupported for audited models")
        state = inspect(obj)
        new, deleted = obj in session.new, obj in session.deleted
        modified = {attr.key for attr in state.attrs if attr.history.has_changes()} - {
            "created",
            "last_updated",
        }
        if not new and not deleted and not modified:
            continue
        if not new and modified & (
            {spec.identity} | {column.key for column in state.mapper.primary_key}
        ):
            raise ValueError("Audited object identities are immutable")
        batch.append((spec, obj, new, deleted, modified))
        if len(batch) == BATCH_SIZE:
            table = capture_batch(session, batch, table)
            batch.clear()
    if batch:
        capture_batch(session, batch, table)


@event.listens_for(Session, "after_flush_postexec")
def refresh_snapshots(session: Session, flush_context: UOWTransaction) -> None:
    """Read authoritative post-SQL values and synchronized FKs in bounded batches.

    Args:
        session: Flushed business session.
        flush_context: Completed SQLAlchemy unit of work.
    """
    refresh = session.info.pop("audit_refresh", [])
    if not refresh:
        return
    # The table was created in this same flush. Savepoint recovery is checked at
    # the first pre-flush batch and again before outer finalization.
    table = cast(Table, session.info["audit_staging"])
    statement = (
        table.update()
        .where(
            table.c.changed_object_type_id == bindparam("_type"),
            table.c.changed_object_id == bindparam("_identity"),
        )
        .values(
            postchange_data=bindparam("_post"),
            related_object_type_id=bindparam("_related_type"),
            related_object_id=bindparam("_related_id"),
        )
    )
    for offset in range(0, len(refresh), BATCH_SIZE):
        batch = refresh[offset : offset + BATCH_SIZE]
        persisted = read_snapshots(session, batch, lock=False)
        rows = []
        for spec, obj in batch:
            snapshot = spec.serialize(obj)
            snapshot.update(persisted[(spec.model, str(getattr(obj, spec.identity)))])
            related_type = related_id = None
            if spec.related is not None:
                field, model = spec.related
                value = getattr(obj, field)
                if value is not None:
                    related_type, related_id = registry[model].type_id, str(value)
            rows.append(
                {
                    "_type": spec.type_id,
                    "_identity": str(getattr(obj, spec.identity)),
                    "_post": snapshot,
                    "_related_type": related_type,
                    "_related_id": related_id,
                }
            )
        session.connection().execute(statement, rows)


@event.listens_for(Session, "before_commit")
def finalize(session: Session) -> None:
    """Insert consolidated immutable history atomically with the outer commit.

    Args:
        session: Committing unit of work; savepoint commits defer finalization.
    """
    if session.in_nested_transaction():
        return
    session.flush()
    if "audit_staging" not in session.info:
        return
    from tinypods_core.db.models import User

    table = staging_table(session)
    users = inspect(User).local_table
    target = cast(Table, inspect(AuditRecord).local_table)
    values = [
        users.c.id
        if column.name == "user_id"
        else case((users.c.id.is_not(None), users.c.username), else_=table.c.user_name)
        if column.name == "user_name"
        else table.c[column.name]
        for column in target.columns
    ]
    meaningful = and_(
        or_(table.c.prechange_data.is_not(None), table.c.postchange_data.is_not(None)),
        or_(
            table.c.action != "update",
            table.c.sensitive,
            table.c.prechange_data.is_distinct_from(table.c.postchange_data),
        ),
    )
    source = (
        select(*values)
        .select_from(table.outerjoin(users, table.c.user_id == users.c.id))
        .where(meaningful)
    )
    session.connection().execute(target.insert().from_select(list(target.columns.keys()), source))


@event.listens_for(Session, "after_transaction_end")
def cleanup(session: Session, transaction: SessionTransaction) -> None:
    """Release Python handles; PostgreSQL owns transactional staging cleanup.

    Args:
        session: Unit-of-work owner.
        transaction: Completed root or nested transaction.
    """
    if transaction.parent is None:
        session.info.pop("audit_staging", None)
        session.info.pop("audit_context", None)
        session.info.pop("audit_refresh", None)


@event.listens_for(Session, "do_orm_execute")
def guard_bulk(state: ORMExecuteState) -> None:
    """Reject ORM/Core bulk DML for audited tables; raw SQL is an operator boundary.

    Args:
        state: SQLAlchemy's session statement execution context.
    """
    guarded = {model.__tablename__ for model in registry} | {"audit_records", "object_types"}
    # Inspect nested CTEs too: a SELECT can contain a data-changing statement.
    for clause in visitors.iterate(cast(ClauseElement, state.statement)):
        if isinstance(clause, (Insert, Update, Delete)):
            table: object = clause.table
            while isinstance(table, Alias):
                table = table.original
            if getattr(table, "name", None) in guarded:
                raise ValueError(
                    "Bulk mutation bypasses object change logging; use the unit of work"
                )
