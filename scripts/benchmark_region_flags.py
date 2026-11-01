"""Benchmark real region flag queries on disposable PostgreSQL, never an operator database.

Run from the repository root with uv run --no-sync python scripts/benchmark_region_flags.py.
The JSON report includes latency distributions, throughput, EXPLAIN buffers/loops,
and concurrent metadata/hierarchy writers. Setup uses the isolated test fixtures.
"""

import argparse
import asyncio
import importlib
import json
import os
import platform
import sys
from collections.abc import Callable, Generator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import text, true
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql.base import Executable
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.sql.expression import Select
from tinypods_api.clusters.contracts import RegionRead
from tinypods_api.http.presentation import (
    Computations,
    Overrides,
    collect,
    loading_options,
    serialize,
)
from tinypods_core.clusters import Region
from tinypods_core.clusters.service import flag_query
from tinypods_core.db import MigrationManager, core_metadata, database_url
from tinypods_core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


class Endpoints(Protocol):
    """Read-only fixture endpoint contract; operator connection strings are not accepted."""

    direct: str
    pooled: str


def isolated_services() -> Generator[Endpoints]:
    """Load the repository's typed disposable-service fixture through its pytest wrapper."""
    fixtures = importlib.import_module("conftest")
    factory = cast(Callable[[], Generator[Endpoints]], fixtures.postgres_endpoints.__wrapped__)
    return factory()


FLAG = "https://flags.example.test/region.svg"
BASELINE = """
WITH RECURSIVE paths AS (
 SELECT id AS origin, id, parent_id, flag, 0 AS depth FROM regions
 WHERE id = ANY(CAST(:ids AS uuid[]))
 UNION ALL
 SELECT a.origin, r.id, r.parent_id, r.flag, a.depth + 1
 FROM paths a JOIN regions r ON r.id = a.parent_id
)
SELECT DISTINCT ON (origin) origin, flag FROM paths
WHERE flag IS NOT NULL ORDER BY origin, depth
"""


def cte_flag_query(identities: list[UUID]) -> Select[UUID, str]:
    """Resolve nearest flags for selected regions while sharing ancestor traversal.

    Args:
        identities: Identifiers from one already authorized, bounded region page.

    Returns:
        One read-only statement yielding an ID and flag for regions with an override
        somewhere on their ancestor path. Missing rows mean no flag is available.
    """
    # UNION deduplicates common ancestors across the page. Stop climbing once an
    # override is found, then propagate each flag down the selected ancestor closure.
    # This avoids traversing a shared deep path once for every returned region.
    ancestors = (
        select(col(Region.id), col(Region.parent_id), col(Region.flag))
        .where(col(Region.id).in_(identities))
        .cte("flag_ancestors", recursive=True)
    )
    # A one-parent lateral lookup keeps PostgreSQL on the primary-key path.
    # Without this boundary, a generic plan can scan the entire region table at
    # every recursion level when a page has many regions sharing a deep ancestor.
    parent = (
        select(col(Region.id), col(Region.parent_id), col(Region.flag))
        .where(col(Region.id) == ancestors.c.parent_id)
        .limit(1)
        .correlate(ancestors)
        .lateral("flag_parent")
    )
    ancestors = ancestors.union(
        select(parent.c.id, parent.c.parent_id, parent.c.flag)
        .select_from(ancestors)
        .join(parent, true())
        .where(ancestors.c.flag.is_(None))
    )
    flags = (
        select(ancestors.c.id, ancestors.c.flag)
        .where(ancestors.c.flag.is_not(None))
        .cte("region_flags", recursive=True)
    )
    flags = flags.union_all(
        select(ancestors.c.id, flags.c.flag)
        .join(flags, ancestors.c.parent_id == flags.c.id)
        .where(ancestors.c.flag.is_(None))
    )
    return select(flags.c.id, flags.c.flag).where(flags.c.id.in_(identities))


def summary(latencies: list[float], elapsed: float) -> dict[str, float | int]:
    """Report measured milliseconds and completed queries per second."""
    values = sorted(latencies)
    return {
        "queries": len(values),
        "p50_ms": values[len(values) // 2],
        "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))],
        "max_ms": max(values),
        "queries_per_second": len(values) / elapsed,
    }


def dataset(shape: str) -> tuple[list[dict[str, object]], list[UUID], UUID, UUID, UUID]:
    """Build valid synthetic trees, returning a page and IDs for controlled writers."""
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []

    def add(parent: UUID | None, flag: str | None = None) -> UUID:
        """Append one valid row with a stable unique sibling identity."""
        identity = uuid4()
        rows.append(
            dict(
                id=identity,
                name=f"Region {len(rows)}",
                slug=f"region-{len(rows)}",
                description="",
                parent_id=parent,
                flag=flag,
                created=now,
                last_updated=now,
            )
        )
        return identity

    root = add(None, FLAG)
    alternate = add(None, FLAG)
    anchor = add(root)
    leaves: list[UUID] = []
    if shape == "wide-10000":
        leaves = [add(anchor) for _ in range(10000)]
    elif shape == "balanced-10000":
        identities = [root, alternate, anchor]
        for index in range(10000):
            identities.append(add(identities[2 + index // 10]))
        leaves = identities[-100:]
    else:
        parent = anchor
        for index in range(1000):
            parent = add(
                parent, FLAG if shape == "overrides-depth-1000" and index % 100 == 0 else None
            )
        leaves = [add(parent) for _ in range(100)]
        if shape == "detail-depth-1000":
            leaves = leaves[-1:]
    return rows, leaves[-100:], root, alternate, anchor


async def exercise(
    engine: AsyncEngine,
    statement: Executable,
    parameters: Mapping[str, object],
    identities: list[UUID],
    concurrency: int,
    repeats: int,
) -> dict[str, float | int]:
    """Measure complete result consumption, including database contention between readers."""
    latencies: list[float] = []

    async def reader() -> None:
        """Reuse a connection like a request worker and verify each complete result."""
        async with engine.connect() as connection:
            for _ in range(repeats):
                started = perf_counter()
                result: dict[UUID, str] = dict(
                    (await connection.execute(statement, parameters)).all()
                )
                assert set(result) == set(identities) and set(result.values()) == {FLAG}
                latencies.append((perf_counter() - started) * 1000)

    started = perf_counter()
    await asyncio.gather(*(reader() for _ in range(concurrency)))
    return summary(latencies, perf_counter() - started)


async def exercise_page(
    engine: AsyncEngine, identities: list[UUID], concurrency: int, repeats: int
) -> dict[str, float | int]:
    """Measure ORM metadata, inherited flags and JSON projection, excluding HTTP/auth."""
    latencies: list[float] = []

    async def reader() -> None:
        """Verify private paths remain deferred and cannot leak into response projection."""
        async with AsyncSession(engine) as session:
            for _ in range(repeats):
                started = perf_counter()
                records = (
                    await session.exec(
                        select(Region)
                        .where(col(Region.id).in_(identities))
                        .options(*loading_options(RegionRead))
                    )
                ).all()
                assert all("path" not in record.__dict__ for record in records)
                computations: Computations = {}
                for record in records:
                    collect(RegionRead, record, computations)
                overrides: Overrides = {}
                for (model, resolver), (members, fields) in computations.items():
                    values = await resolver(session, list(members.values()), frozenset(fields))
                    overrides.update({(model, key): value for key, value in values.items()})
                payload = [
                    RegionRead.model_validate(serialize(RegionRead, record, overrides)).model_dump(
                        mode="json"
                    )
                    for record in records
                ]
                assert all("path" not in item and item["flag"] == FLAG for item in payload)
                json.dumps(payload)
                latencies.append((perf_counter() - started) * 1000)

    started = perf_counter()
    await asyncio.gather(*(reader() for _ in range(concurrency)))
    return summary(latencies, perf_counter() - started)


async def explain(
    engine: AsyncEngine, sql: str, parameters: Mapping[str, object]
) -> dict[str, Any]:
    """Collect actual query plans, buffers and recursive loops, not just planner estimates."""
    async with engine.connect() as connection:
        plan = (
            await connection.execute(
                text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), parameters
            )
        ).scalar_one()[0]
    return cast(dict[str, Any], plan)


async def write_regions(
    engine: AsyncEngine, root: UUID, alternate: UUID, anchor: UUID
) -> list[float]:
    """Measure metadata and hierarchy edits while independent readers query flags."""
    latencies: list[float] = []
    for index in range(30):
        started = perf_counter()
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE regions SET parent_id=:parent WHERE id=:id"),
                {"parent": root if index % 2 else alternate, "id": anchor},
            )
            await connection.execute(
                text("UPDATE regions SET flag=:flag WHERE id=:id"), {"flag": FLAG, "id": root}
            )
        latencies.append((perf_counter() - started) * 1000)
    return latencies


async def benchmark(settings: Settings, prepare: bool) -> dict[str, object]:
    """Compare both query shapes across width, depth, overrides and concurrent writers."""
    engine = create_async_engine(
        database_url(settings),
        pool_size=40,
        max_overflow=0,
        connect_args={
            "options": "-c statement_timeout=15000",
            "prepare_threshold": 5 if prepare else None,
        },
    )
    report: dict[str, object] = {
        "prepared_statements": prepare,
        "cpu_count": os.cpu_count(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "datasets": [],
    }
    datasets: list[dict[str, object]] = []
    try:
        async with engine.connect() as connection:
            report["postgres"] = (await connection.execute(text("SELECT version()"))).scalar_one()
        for shape in (
            "wide-10000",
            "balanced-10000",
            "depth-1000",
            "detail-depth-1000",
            "overrides-depth-1000",
        ):
            rows, identities, root, alternate, anchor = dataset(shape)
            paths: dict[UUID, list[UUID]] = {}
            for row in rows:
                identity = cast(UUID, row["id"])
                parent_id = cast(UUID | None, row["parent_id"])
                paths[identity] = [*paths.get(parent_id, []), identity] if parent_id else [identity]
                row["path"] = paths[identity]
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE clusters, regions"))
                # Fixtures are known-valid trees. Disable only their user triggers
                # during bulk setup, excluding seed validation from read timings.
                await connection.execute(text("ALTER TABLE regions DISABLE TRIGGER USER"))
                await connection.execute(core_metadata.tables["regions"].insert(), rows)
                await connection.execute(text("ALTER TABLE regions ENABLE TRIGGER USER"))
                await connection.execute(text("ANALYZE regions"))
            optimized = flag_query(identities)
            compiled = str(
                optimized.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
            )
            case: dict[str, object] = {
                "shape": shape,
                "nodes": len(rows),
                "page_size": len(identities),
            }
            for name, statement, parameters, sql in (
                ("baseline", text(BASELINE), {"ids": identities}, BASELINE),
                (
                    "indexed_cte",
                    cte_flag_query(identities),
                    {},
                    str(
                        cte_flag_query(identities).compile(
                            dialect=engine.dialect, compile_kwargs={"literal_binds": True}
                        )
                    ),
                ),
                ("indexed_path", optimized, {}, compiled),
            ):
                await exercise(engine, statement, parameters, identities, 1, 3)
                measurements = []
                for concurrency, repeats in ((1, 20), (8, 8), (32, 4)):
                    measured = await exercise(
                        engine, statement, parameters, identities, concurrency, repeats
                    )
                    measurements.append({"concurrency": concurrency, **measured})
                case[name] = {
                    "reads": measurements,
                    "explain": await explain(engine, sql, parameters),
                }
            case["page_projection"] = [
                {
                    "concurrency": concurrency,
                    **await exercise_page(engine, identities, concurrency, 4),
                }
                for concurrency in (1, 8, 32)
            ]
            # Concurrent edits move the common subtree between two roots that have
            # the same flag, allowing every reader to assert a coherent result.
            started = perf_counter()
            write_latencies, concurrent_reads = await asyncio.gather(
                write_regions(engine, root, alternate, anchor),
                exercise(engine, optimized, {}, identities, 16, 8),
            )
            case["concurrent_writer"] = {
                "reads": concurrent_reads,
                "writes": summary(write_latencies, perf_counter() - started),
            }
            started = perf_counter()
            writes_and_reads = await asyncio.gather(
                *(write_regions(engine, root, alternate, anchor) for _ in range(4)),
                exercise(engine, optimized, {}, identities, 16, 8),
            )
            case["four_writers"] = {
                "reads": writes_and_reads[-1],
                "writes": [
                    summary(cast(list[float], values), perf_counter() - started)
                    for values in writes_and_reads[:-1]
                ],
            }
            # Hold an uncommitted flag edit: ordinary reads must use MVCC rather
            # than waiting for the writer's tuple lock or exposing dirty metadata.
            async with engine.connect() as held:
                transaction = await held.begin()
                try:
                    await held.execute(
                        text("UPDATE regions SET flag=:flag WHERE id=:id"),
                        {"flag": "https://flags.example.test/uncommitted.svg", "id": root},
                    )
                    case["held_writer_reads"] = await asyncio.wait_for(
                        exercise(engine, optimized, {}, identities, 8, 4), timeout=15
                    )
                finally:
                    await transaction.rollback()
            async with engine.connect() as held:
                transaction = await held.begin()
                try:
                    await held.execute(
                        text("UPDATE regions SET parent_id=:parent WHERE id=:id"),
                        {"parent": alternate, "id": anchor},
                    )
                    case["held_move_reads"] = await asyncio.wait_for(
                        exercise(engine, optimized, {}, identities, 8, 4), timeout=15
                    )
                finally:
                    await transaction.rollback()
            datasets.append(case)
            print(json.dumps({"completed": shape, "nodes": len(rows)}), flush=True)
        report["datasets"] = datasets
        return report
    finally:
        await engine.dispose()


def main() -> None:
    """Create and clean isolated services, then save a reproducible benchmark artifact."""
    resources = isolated_services()
    endpoints = next(resources)
    try:
        settings = Settings(database_url=SecretStr(endpoints.direct))
        MigrationManager(settings, ROOT / "alembic.ini").upgrade()
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--unprepared",
            action="store_true",
            help="Disable auto-prepare, as in transaction pooling",
        )
        arguments = parser.parse_args()
        report = asyncio.run(benchmark(settings, not arguments.unprepared))
        target = ROOT / (
            "reports/benchmarks/region-flags-unprepared.json"
            if arguments.unprepared
            else "reports/benchmarks/region-flags.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2))
        print(str(target))
    finally:
        resources.close()


if __name__ == "__main__":
    main()
