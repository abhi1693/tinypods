"""Declarative nested schemas and bounded SQL on real linked PostgreSQL records."""

import asyncio
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import event
from sqlmodel import select
from test_auth import auth_settings as auth_settings
from test_auth import credentials
from tinypods_api.clusters.contracts import ClusterRead, ClusterWrite, RegionRead
from tinypods_api.http.presentation import (
    Presenter,
    identity,
    loaded,
    loading_options,
    relation_type,
    serialize,
)
from tinypods_api.http.representations import Related, ResourceRead, related_schema
from tinypods_core.auth import SessionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.clusters import Cluster, Region
from tinypods_core.db import Database
from tinypods_core.persistence import CoreModel
from tinypods_core.settings import Settings


def test_brief_schema_public_fallback_and_validation() -> None:
    """Missing/empty declarations reuse public DTOs, never private ORM fields."""

    class Model(CoreModel):
        brief_fields: ClassVar[tuple[str, ...] | None] = None
        id: UUID
        private: str

    class Public(ResourceRead):
        resource_model = Model
        id: UUID

    assert Related[Public] is Public
    Model.brief_fields = ()
    related_schema.cache_clear()
    assert Related[Public] is Public
    Model.brief_fields = ("id",)
    related_schema.cache_clear()
    brief = Related[Public]
    assert set(brief.model_fields) == {"id"}
    assert Related[Public] is brief
    assert "private" not in brief.model_json_schema()["properties"]
    Model.brief_fields = ("private",)
    related_schema.cache_clear()
    with pytest.raises(ValueError, match="Unknown brief"):
        Related[Public]
    with pytest.raises(ValueError, match="public representation"):

        class Invalid(ResourceRead):
            resource_model = Model
            id: UUID

    Model.brief_fields = ("id", "id")
    with pytest.raises(ValueError, match="distinct"):

        class Duplicate(ResourceRead):
            resource_model = Model
            id: UUID

    with pytest.raises(TypeError, match="one public"):
        Related[str]
    with pytest.raises(ValueError, match="registered public"):
        Related[ResourceRead]
    assert relation_type(str | int) is None
    assert relation_type(list[str]) is None
    assert relation_type(list[ClusterRead]) == (ClusterRead, True)
    assert relation_type(RegionRead | None) == (RegionRead, False)
    with pytest.raises(ValueError, match="no public resource"):
        relation_type(ResourceRead)
    with pytest.raises(ValueError, match="finite registered"):
        loading_options(ResourceRead)
    with pytest.raises(ValueError, match="finite registered"):
        loading_options(ClusterRead, (ClusterRead,))
    with pytest.raises(ValueError, match="persisted"):
        identity(Region(name="Unstored", slug="unstored"))
    with pytest.raises(ValidationError):
        ClusterWrite.model_validate(
            {"name": "Wrong", "slug": "wrong", "region": {"id": str(uuid4())}}
        )


def test_prefetch_page_queries_and_detached_projection(auth_settings: Settings) -> None:
    """One and 100 related records use identical SQL counts; serialization uses none."""

    async def scenario() -> None:
        database = Database(auth_settings)
        try:
            owner = await LocalPasswordProvider(database).bootstrap(credentials())
            sessions = SessionService(database)
            principal = (await sessions.issue(owner)).principal
            authorization = AuthorizationService(sessions)
            async with database.session() as session:
                root = Region(name="India", slug="india", flag="https://example.test/india.svg")
                session.add(root)
                await session.flush()
                child = Region(name="Gurugram", slug="gurugram", parent_id=root.id)
                session.add(child)
                await session.flush()
                session.add_all(
                    [
                        Cluster(
                            name=f"Cluster {index}", slug=f"cluster-{index}", region_id=child.id
                        )
                        for index in range(100)
                    ]
                )
                await session.commit()
            counts = []
            for size in (1, 100):
                async with database.session() as session:
                    rows = (await session.exec(select(Cluster).limit(size))).all()
                    statements = []
                    connection = (await session.connection()).sync_connection

                    def capture(
                        conn: object,
                        cursor: object,
                        statement: str,
                        parameters: object,
                        context: object,
                        executemany: bool,
                        captured: list[str] = statements,
                    ) -> None:
                        """Count actual statements on this request connection."""
                        captured.append(statement)

                    event.listen(connection, "before_cursor_execute", capture)
                    try:
                        presenter = Presenter(session, authorization)
                        output = await presenter.many(principal, ClusterRead, rows)
                        counts.append(len(statements))
                        assert len(output) == size
                        assert all(item.region.flag == root.flag for item in output)
                        assert all(item.region.id == child.id for item in output)
                        assert all("path" not in item.model_dump_json() for item in output)
                        graph = await presenter._load(ClusterRead, [identity(row) for row in rows])
                        before = len(statements)
                        detached = [serialize(ClusterRead, row, {}) for row in graph.values()]
                        assert len(detached) == size and len(statements) == before
                        with pytest.raises(ValueError, match="not prefetched"):
                            loaded(next(iter(graph.values())).region, "description")
                    finally:
                        event.remove(connection, "before_cursor_execute", capture)
            assert counts[0] == counts[1]
            assert counts[0] <= 4
            async with database.session() as session:
                from tinypods_api.clusters.computed import region_values

                assert await region_values(session, [], frozenset({"id"})) == {}
                presenter = Presenter(session, authorization)
                assert await presenter.many(principal, ClusterRead, []) == []

                class Action(BaseModel):
                    region: Related[RegionRead] | None
                    total: int

                result = await presenter.values(
                    principal, Action, {"region_id": child.id, "total": 100}
                )
                assert result.region.flag == root.flag
                assert (
                    await presenter.values(principal, Action, {"region_id": None, "total": 0})
                ).region is None
                with pytest.raises(ValueError, match="UUIDs"):
                    await presenter.values(principal, Action, {"region_id": "bad", "total": 0})

                class CollectionAction(BaseModel):
                    regions: list[Related[RegionRead]]

                with pytest.raises(ValueError, match="single related"):
                    await presenter.values(principal, CollectionAction, {})
        finally:
            await database.close()

    asyncio.run(scenario())


def test_collection_prefetch_and_graph_validation(auth_settings: Settings) -> None:
    """The shared loader batches collection relations and rejects invalid graphs."""
    from sqlalchemy import ForeignKey
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
    from tinypods_core.applications.service import DefinitionError
    from tinypods_core.auth import Principal

    class FixtureBase(DeclarativeBase):
        """Keep fixture tables out of application metadata and migrations."""

    class Group(FixtureBase):
        __tablename__ = "presentation_test_group"
        id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
        children: Mapped[list["Member"]] = relationship(lazy="raise", cascade="all, delete-orphan")
        brief_fields = None

    class Member(FixtureBase):
        __tablename__ = "presentation_test_member"
        id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
        group_id: Mapped[UUID] = mapped_column(ForeignKey("presentation_test_group.id"))
        brief_fields = None

    class MemberRead(ResourceRead):
        resource_model = Member
        id: UUID

    class GroupRead(ResourceRead):
        resource_model = Group
        id: UUID
        children: list[MemberRead]

    class WrongTarget(ResourceRead):
        resource_model = Group
        children: list[RegionRead]

    class WrongCardinality(ResourceRead):
        resource_model = Group
        children: MemberRead

    class UnknownScalar(ResourceRead):
        resource_model = Group
        missing: str

    for schema, message in (
        (WrongTarget, "mapped target"),
        (WrongCardinality, "cardinality"),
        (UnknownScalar, "scalar field"),
    ):
        with pytest.raises(ValueError, match=message):
            loading_options(schema)

    class Validator:
        async def validate_principal(self, principal: Principal) -> Principal:
            """Use a trusted internal test identity without extra SQL."""
            return principal

    async def scenario() -> None:
        database = Database(auth_settings)
        try:
            async with database.session() as session:
                connection = await session.connection()
                await connection.run_sync(FixtureBase.metadata.create_all)
                groups = [Group(children=[Member(), Member()]) for _ in range(100)]
                session.add_all(groups)
                await session.commit()
            principal = Principal(uuid4(), "test", True, "test", credential_kind="test")
            authorization = AuthorizationService(Validator())
            counts = []
            for size in (1, 100):
                async with database.session() as session:
                    statements: list[str] = []
                    connection = (await session.connection()).sync_connection

                    def capture(
                        conn: object,
                        cursor: object,
                        statement: str,
                        parameters: object,
                        context: object,
                        executemany: bool,
                        captured: list[str] = statements,
                    ) -> None:
                        """Record collection graph SQL without counting fixture setup."""
                        captured.append(statement)

                    event.listen(connection, "before_cursor_execute", capture)
                    try:
                        presenter = Presenter(session, authorization)
                        records = await presenter._load(
                            GroupRead, [(group.id,) for group in groups[:size]]
                        )
                        counts.append(len(statements))
                        projected = [
                            GroupRead.model_validate(serialize(GroupRead, record, {}))
                            for record in records.values()
                        ]
                        assert len(projected) == size
                        assert all(len(item.children) == 2 for item in projected)
                        assert len(statements) == counts[-1]
                        computations = {}
                        from tinypods_api.http.presentation import collect

                        for record in records.values():
                            collect(GroupRead, record, computations)
                        assert computations == {}
                    finally:
                        event.remove(connection, "before_cursor_execute", capture)
            assert counts == [2, 2]
            async with database.session() as session:
                presenter = Presenter(session, authorization)

                class Action(BaseModel):
                    region: Related[RegionRead]

                with pytest.raises(DefinitionError) as absent:
                    await presenter.values(principal, Action, {"region_id": uuid4()})
                assert absent.value.status == 404
                async with database.session() as other:
                    record = await other.get(Group, groups[0].id)
                    await other.delete(record)
                    await other.commit()
                with pytest.raises(DefinitionError) as missing:
                    await presenter.many(principal, GroupRead, [groups[0]])
                assert missing.value.status == 404
        finally:
            await database.close()

    asyncio.run(scenario())


def test_catalogue_graphs_keep_query_counts_flat(auth_settings: Settings) -> None:
    """Compare deep release and composite membership graphs at one/100 rows."""
    import json
    from time import perf_counter

    from tinypods_api.applications.contracts import MembershipRead, ReleaseRead
    from tinypods_core.applications import Application, ApplicationCategory, Category, Release
    from tinypods_core.repositories import Chart, ChartRepository, ChartVersion

    async def scenario() -> None:
        database = Database(auth_settings)
        try:
            owner = await LocalPasswordProvider(database).bootstrap(credentials())
            sessions = SessionService(database)
            principal = (await sessions.issue(owner)).principal
            authorization = AuthorizationService(sessions)
            async with database.transaction() as session:
                app = Application(name="Catalogue", slug="catalogue")
                repo = ChartRepository(name="Public", url="https://example.test/charts")
                session.add_all([app, repo])
                await session.flush()
                chart = Chart(repository_id=repo.id, name="database")
                session.add(chart)
                await session.flush()
                categories = [
                    Category(name=f"Category {i}", slug=f"category-{i}") for i in range(100)
                ]
                versions = [
                    ChartVersion(chart_id=chart.id, version=f"1.0.{i}", digest="sha256:" + "a" * 64)
                    for i in range(100)
                ]
                session.add_all(categories + versions)
                await session.flush()
                session.add_all(
                    [
                        Release(application_id=app.id, chart_version_id=version.id)
                        for version in versions
                    ]
                )
                session.add_all(
                    [
                        ApplicationCategory(application_id=app.id, category_id=category.id)
                        for category in categories
                    ]
                )
            metrics = []
            for model, schema in ((Release, ReleaseRead), (ApplicationCategory, MembershipRead)):
                counts = []
                for size in (1, 100):
                    async with database.session() as session:
                        rows = (await session.exec(select(model).limit(size))).all()
                        statements: list[str] = []
                        connection = (await session.connection()).sync_connection

                        def capture(
                            conn: object,
                            cursor: object,
                            statement: str,
                            parameters: object,
                            context: object,
                            executemany: bool,
                            captured: list[str] = statements,
                        ) -> None:
                            """Measure actual eager-load statements at each page size."""
                            captured.append(statement)

                        event.listen(connection, "before_cursor_execute", capture)
                        try:
                            started = perf_counter()
                            output = await Presenter(session, authorization).many(
                                principal, schema, rows
                            )
                            elapsed = (perf_counter() - started) * 1000
                            counts.append(len(statements))
                            assert len(output) == size
                            payload = json.dumps([item.model_dump(mode="json") for item in output])
                            metrics.append(
                                {
                                    "resource": model.__name__,
                                    "rows": size,
                                    "queries": len(statements),
                                    "milliseconds": round(elapsed, 2),
                                    "bytes": len(payload.encode()),
                                }
                            )
                            if model is Release:
                                assert all(
                                    item.chart_version.chart.name == "database" for item in output
                                )
                            else:
                                assert all(item.application.name == "Catalogue" for item in output)
                        finally:
                            event.remove(connection, "before_cursor_execute", capture)
                assert counts[0] == counts[1]
                assert counts[0] <= 3
            print(json.dumps({"nested_page_measurements": metrics}))
        finally:
            await database.close()

    asyncio.run(scenario())
