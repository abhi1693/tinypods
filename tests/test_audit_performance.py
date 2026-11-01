"""Reproducible audit costs on disposable PostgreSQL, without timing thresholds.

Run `uv run --no-sync pytest tests/test_audit_performance.py -s --no-cov`.
The complete transaction measurements include ORM ownership, snapshot reads,
staging catalog checks/DDL, batches and finalization, not just serialization.
"""

import asyncio
import json
import platform
import time
import tracemalloc
from collections import Counter
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import MetaData, event
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlmodel import Field, Relationship, select
from test_audit_trail import audit_settings as audit_settings
from tinypods_core.audit import AuditRecord, AuditTrailMixin, ModelRegistration, register
from tinypods_core.audit.registry import registry
from tinypods_core.db import Database
from tinypods_core.db.models import User
from tinypods_core.settings import Settings

_metadata = MetaData()


class PerformanceParent(AuditTrailMixin, table=True):
    """Mapped transient parent used only to measure declared relationship snapshots."""

    __tablename__ = "audit_perf_parent"
    metadata: ClassVar[MetaData] = _metadata
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    members: list["PerformanceMember"] = Relationship(back_populates="parent")


class PerformanceMember(AuditTrailMixin, table=True):
    """Mapped transient member; no benchmark tables or operator data are created."""

    __tablename__ = "audit_perf_member"
    metadata: ClassVar[MetaData] = _metadata
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    parent_id: UUID | None = Field(default=None, foreign_key="audit_perf_parent.id")
    parent: PerformanceParent | None = Relationship(back_populates="members")


def test_reproducible_audit_costs(audit_settings: Settings) -> None:
    """Measure initialization, wide serialization and >1000-object runtime transactions.

    Args:
        audit_settings: Ephemeral PostgreSQL; never an environment-provided database.
    """
    metrics: dict[str, object] = {
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "objects": 1200,
    }
    counts: Counter[str] = Counter()

    def query(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        """Count actual cursor calls by purpose without collecting any parameters."""
        normalized = statement.lower().strip()
        counts["cursor_calls"] += 1
        if "pg_catalog" in normalized:
            counts["catalog_checks"] += 1
        if normalized.startswith("create temporary table"):
            counts["temporary_tables"] += 1
        if normalized.startswith("select") and "from auth_user" in normalized:
            counts["user_selects"] += 1
        if normalized.startswith("insert into audit_stage_"):
            counts["staging_upserts"] += 1
        if normalized.startswith("update audit_stage_"):
            counts["staging_updates"] += 1
        if normalized.startswith("insert into audit_records"):
            counts["finalizations"] += 1

    async def measure(database: Database, audited: bool, update_rows: bool) -> None:
        """Include model construction, loading and commit in runtime measurements."""
        key = ("audited" if audited else "baseline") + ("_update" if update_rows else "_insert")
        original = registry[User]
        if not audited:
            registry.pop(User)
        counts.clear()
        tracemalloc.start()
        started = time.perf_counter()
        try:
            async with database.transaction() as session:
                prefix = "audited-" if audited else "baseline-"
                if update_rows:
                    users = (
                        await session.exec(select(User).where(User.username.startswith(prefix)))
                    ).all()
                    for user in users:
                        user.is_owner = True
                else:
                    session.add_all(User(username=prefix + str(index)) for index in range(1200))
                await session.flush()
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            metrics[key] = {"seconds": elapsed, "peak_traced_bytes": peak, **counts}
            if audited:
                assert counts["finalizations"] == 1
                assert counts["staging_upserts"] == 12
                assert counts["staging_updates"] == 12
                assert counts["user_selects"] <= (25 if update_rows else 12)
                assert counts["catalog_checks"] <= 2
            else:
                assert counts["finalizations"] == 0
        finally:
            tracemalloc.stop()
            registry[User] = original

    async def scenario() -> None:
        """Own database resources for all measurements on one warmed runtime engine."""
        database = Database(audit_settings)
        try:
            assert await database.ping()
            event.listen(database.engine.sync_engine, "before_cursor_execute", query)
            for audited, update_rows in [
                (False, False),
                (True, False),
                (False, True),
                (True, True),
            ]:
                await measure(database, audited, update_rows)
            async with database.session() as session:
                assert len((await session.exec(select(AuditRecord))).all()) == 2400
        finally:
            await database.close()

    asyncio.run(scenario())
    counts.clear()
    started = time.perf_counter()
    for _ in range(10000):
        ModelRegistration(User, "performance", "User", ("id", "username"))
    metrics["registration_initialization"] = {
        "iterations": 10000,
        "seconds": time.perf_counter() - started,
        "cursor_calls": counts["cursor_calls"],
    }
    assert counts["cursor_calls"] == 0

    registration = ModelRegistration(User, "performance", "HotPath", ("id",))
    first_type_id = registration.type_id
    started = time.perf_counter()
    for _ in range(10000):
        assert registration.type_id is first_type_id
    metrics["cached_type_identity"] = {
        "iterations": 10000,
        "seconds": time.perf_counter() - started,
        "cursor_calls": counts["cursor_calls"],
    }

    parent_spec = ModelRegistration(
        PerformanceParent, "performance", "Parent", ("id",), relationships=("members",)
    )
    register(parent_spec)
    register(ModelRegistration(PerformanceMember, "performance", "Member", ("id",)))
    try:
        parent = PerformanceParent(members=[PerformanceMember() for _ in range(10000)])
        counts.clear()
        tracemalloc.start()
        started = time.perf_counter()
        for _ in range(10):
            snapshot = parent.serialize_object()
            assert len(snapshot["members"]) == 10000
        _, peak = tracemalloc.get_traced_memory()
        metrics["relationship_serialization"] = {
            "members": 10000,
            "iterations": 10,
            "seconds": time.perf_counter() - started,
            "peak_traced_bytes": peak,
            "cursor_calls": counts["cursor_calls"],
        }
        assert counts["cursor_calls"] == 0
    finally:
        tracemalloc.stop()
        registry.pop(PerformanceParent)
        registry.pop(PerformanceMember)
    output = Path(__file__).resolve().parents[1] / "reports/audit-performance.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
