"""Region hierarchy, visual inheritance and cluster identity on disposable PostgreSQL."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from conftest import PostgresEndpoints
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import select
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from tinypods_api.main import create_app
from tinypods_core.applications.service import DefinitionError
from tinypods_core.audit import AuditRecord
from tinypods_core.auth import SessionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.clusters import Region
from tinypods_core.clusters.service import RegionService
from tinypods_core.db import Database, MigrationManager
from tinypods_core.db.models import User
from tinypods_core.settings import Settings

BASE = "/api/v1"
FLAG = "https://flags.example.test/india.svg"
OTHER_FLAG = "https://flags.example.test/local.svg"
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def client_for(settings: Settings) -> Iterator[TestClient]:
    """Bootstrap a disposable owner and issue a real authenticated browser session."""

    async def seed() -> None:
        database = Database(settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(seed())
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get(BASE + "/regions").status_code == 401
        assert client.get(BASE + "/clusters").status_code == 401
        assert (
            client.post(
                BASE + "/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        client.headers["x-csrf-token"] = client.cookies["__Host-tinypods-csrf"]
        yield client


def create(client: TestClient, resource: str, **payload: object) -> dict[str, Any]:
    """Create through the public API and return its validated JSON response."""
    response = client.post(BASE + "/" + resource, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_region_tree_flags_and_filters(auth_settings: Settings) -> None:
    """Resolve nearest overrides on every read without writing inherited values."""
    with client_for(auth_settings) as client:
        assert client.get(BASE + "/regions").json()["items"] == []
        india = create(client, "regions", name="India", slug="india", flag=FLAG)
        us = create(client, "regions", name="United States", slug="us")
        city = create(client, "regions", name="Gurugram", slug="gurugram", parent_id=india["id"])
        child = create(client, "regions", name="Local", slug="local", parent_id=city["id"])
        assert city["flag"] == child["flag"] == FLAG
        path = BASE + "/regions/" + city["id"]
        child_path = BASE + "/regions/" + child["id"]
        assert client.patch(path, json={"flag": OTHER_FLAG}).json()["flag"] == OTHER_FLAG
        assert client.get(child_path).json()["flag"] == OTHER_FLAG
        flags = {row["id"]: row["flag"] for row in client.get(BASE + "/regions").json()["items"]}
        assert flags[india["id"]] == FLAG
        assert flags[city["id"]] == flags[child["id"]] == OTHER_FLAG
        assert flags[us["id"]] is None
        assert client.patch(path, json={"flag": None}).json()["flag"] == FLAG
        assert (
            client.patch(BASE + "/regions/" + india["id"], json={"flag": OTHER_FLAG}).status_code
            == 200
        )
        assert client.get(child_path).json()["flag"] == OTHER_FLAG
        assert client.patch(path, json={"parent_id": us["id"]}).json()["flag"] is None
        assert client.get(child_path).json()["flag"] is None
        assert client.patch(path, json={"parent_id": None}).json()["parent_id"] is None
        assert (
            client.put(
                path,
                json={
                    "name": "Gurugram",
                    "slug": "gurugram",
                    "parent_id": india["id"],
                    "flag": FLAG,
                },
            ).status_code
            == 200
        )
        assert client.get(BASE + "/regions?roots=true").json()["total"] == 2
        assert (
            client.get(BASE + "/regions?parent_id=" + india["id"]).json()["items"][0]["id"]
            == city["id"]
        )
        assert client.get(BASE + "/regions?slug=gurugram").json()["total"] == 1
        assert (
            client.get(BASE + "/regions?q=GURU&ordering=-name&limit=1").json()["items"][0]["id"]
            == city["id"]
        )
        assert client.get(BASE + "/regions?roots=true&parent_id=" + india["id"]).status_code == 422
        assert client.get(BASE + "/regions?q=absent").json()["items"] == []
        for payload in (
            {"name": None},
            {"description": None},
            {"slug": None},
            {"flag": "file:///tmp/private"},
            {"flag": "https://user:secret@example.test/x"},
            {"flag": "https://user@example.test/x"},
            {"unknown": "x"},
        ):
            assert client.patch(path, json=payload).status_code == 422
        assert client.patch(path, json={"flag": None}).status_code == 200
        assert client.get(BASE + "/regions/" + str(uuid4())).status_code == 404
        assert client.patch(BASE + "/regions/" + str(uuid4()), json={}).status_code == 404
        assert client.get(BASE + "/regions?unexpected=1").status_code == 422

    async def stored() -> None:
        database = Database(auth_settings)
        try:
            async with database.session() as session:
                record = await session.get(Region, UUID(child["id"]))
                assert record is not None and record.flag is None
                records = (
                    await session.exec(
                        select(AuditRecord).where(AuditRecord.changed_object_id == city["id"])
                    )
                ).all()
                assert any(event.action == "update" for event in records)
        finally:
            await database.close()

    asyncio.run(stored())


def test_region_and_cluster_constraints(auth_settings: Settings) -> None:
    """Protect hierarchy and region-local identities while allowing identical names elsewhere."""
    with client_for(auth_settings) as client:
        root = create(client, "regions", name="India", slug="india")
        other = create(client, "regions", name="US", slug="us")
        city = create(client, "regions", name="Gurugram", slug="local", parent_id=root["id"])
        elsewhere = create(
            client, "regions", name="California", slug="local", parent_id=other["id"]
        )
        assert (
            client.post(BASE + "/regions", json={"name": "Duplicate", "slug": "india"}).status_code
            == 409
        )
        assert (
            client.post(
                BASE + "/regions",
                json={"name": "Duplicate", "slug": "local", "parent_id": root["id"]},
            ).status_code
            == 409
        )
        assert (
            client.patch(
                BASE + "/regions/" + root["id"], json={"parent_id": city["id"]}
            ).status_code
            == 409
        )
        assert (
            client.patch(
                BASE + "/regions/" + city["id"], json={"parent_id": city["id"]}
            ).status_code
            == 409
        )
        assert (
            client.patch(
                BASE + "/regions/" + city["id"], json={"parent_id": str(uuid4())}
            ).status_code
            == 409
        )
        first = create(client, "clusters", name="Cluster 1", slug="cluster-1", region_id=city["id"])
        second = create(
            client, "clusters", name="Cluster 1", slug="cluster-1", region_id=elsewhere["id"]
        )
        assert first["id"] != second["id"]
        path = BASE + "/clusters/" + second["id"]
        for payload in (
            {"name": "Cluster 1", "slug": "other", "region_id": city["id"]},
            {"name": "Other", "slug": "cluster-1", "region_id": city["id"]},
        ):
            assert client.post(BASE + "/clusters", json=payload).status_code == 409
        assert client.patch(path, json={"region_id": city["id"]}).status_code == 409
        assert client.patch(path, json={"region_id": None}).status_code == 422
        assert client.patch(path, json={"region_id": str(uuid4())}).status_code == 409
        assert client.get(path).json()["region_id"] == elsewhere["id"]
        assert client.get(BASE + "/clusters?region_id=" + city["id"]).json()["total"] == 1
        assert client.get(BASE + "/clusters?slug=cluster-1").json()["total"] == 2
        assert (
            client.get(BASE + "/clusters?q=cluster&ordering=-created&limit=1").json()["limit"] == 1
        )
        assert client.get(BASE + "/clusters/" + str(uuid4())).status_code == 404
        assert (
            client.patch(
                path, json={"name": "Cluster 2", "slug": "cluster-2", "region_id": city["id"]}
            ).status_code
            == 200
        )
        assert (
            client.put(
                path, json={"name": "Cluster 2", "slug": "cluster-2", "region_id": elsewhere["id"]}
            ).status_code
            == 200
        )
        assert client.delete(BASE + "/regions/" + root["id"]).status_code == 409
        assert client.delete(BASE + "/regions/" + city["id"]).status_code == 409
        assert client.delete(BASE + "/clusters/" + first["id"]).status_code == 204
        assert client.delete(BASE + "/regions/" + city["id"]).status_code == 204
        assert client.delete(BASE + "/regions/" + root["id"]).status_code == 204
        assert client.delete(BASE + "/clusters/" + str(uuid4())).status_code == 404


@pytest.mark.parametrize("pooled", [False, True])
def test_concurrent_region_parent_changes(
    auth_settings: Settings, postgres_endpoints: PostgresEndpoints, pooled: bool
) -> None:
    """Database trigger rejects a cycle even when separate transactions race to form it."""

    async def seed() -> tuple[UUID, UUID]:
        database = Database(auth_settings)
        try:
            async with database.transaction() as session:
                first, second = Region(name="A", slug="a"), Region(name="B", slug="b")
                session.add(first)
                session.add(second)
            return first.id, second.id
        finally:
            await database.close()

    first, second = asyncio.run(seed())
    url = postgres_endpoints.pooled if pooled else postgres_endpoints.direct

    def move(pair: tuple[UUID, UUID]) -> int:
        """Attempt one direct SQL edit; the database owns serialization and cycle validation."""
        try:
            with psycopg.connect(
                url, autocommit=True, prepare_threshold=None if pooled else 5
            ) as connection:
                connection.execute("SET statement_timeout = '10s'")
                connection.execute(
                    "UPDATE regions SET parent_id=%s WHERE id=%s", (pair[1], pair[0])
                )
            return 200
        except psycopg.errors.CheckViolation:
            return 409

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(move, [(first, second), (second, first)]))
    assert sorted(results) == [200, 409]
    with psycopg.connect(postgres_endpoints.direct) as connection:
        assert (
            connection.execute("SELECT count(*) FROM regions WHERE parent_id IS NULL").fetchone()[0]
            == 1
        )


def test_registry_migration_roundtrip(auth_settings: Settings) -> None:
    """Schema and trigger round-trip without changing pre-existing instance identity."""
    manager = MigrationManager(auth_settings, ROOT / "alembic.ini")
    manager.check()
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        identity = connection.execute(
            "SELECT installation_id FROM platform_installation"
        ).fetchone()[0]
    command.downgrade(manager.config(), "0005_chart_repositories")
    manager.upgrade()
    manager.check()
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        assert (
            connection.execute("SELECT installation_id FROM platform_installation").fetchone()[0]
            == identity
        )
        assert connection.execute("SELECT count(*) FROM regions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM clusters").fetchone()[0] == 0


def test_non_owner_registry_access(auth_settings: Settings) -> None:
    """An authenticated ordinary account can browse but cannot change shared infrastructure."""

    async def seed() -> tuple[str, str]:
        database = Database(auth_settings)
        try:
            async with database.transaction() as session:
                user = User(username="reader")
                session.add(user)
                await session.flush()
                identity = user.id
            issued = await SessionService(database).issue(identity)
            return issued.token, issued.csrf_token
        finally:
            await database.close()

    token, csrf = asyncio.run(seed())
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        client.cookies.set("__Host-tinypods-session", token)
        client.cookies.set("__Host-tinypods-csrf", csrf)
        for resource in ("regions", "clusters"):
            assert client.get(BASE + "/" + resource).status_code == 200
            payload: dict[str, object] = {"name": "Denied", "slug": "denied"}
            if resource == "clusters":
                payload["region_id"] = str(uuid4())
            for method, path, body in (
                ("post", "", payload),
                ("patch", "/" + str(uuid4()), {"name": "Denied"}),
                ("delete", "/" + str(uuid4()), None),
            ):
                response = client.request(
                    method, BASE + "/" + resource + path, json=body, headers={"x-csrf-token": csrf}
                )
                assert response.status_code == 403


@pytest.mark.parametrize("pooled", [False, True])
def test_insert_during_subtree_move(
    auth_settings: Settings, postgres_endpoints: PostgresEndpoints, pooled: bool
) -> None:
    """Concurrent insertion cannot escape the atomic path cascade of a subtree move."""

    async def seed() -> tuple[UUID, UUID, UUID]:
        database = Database(auth_settings)
        try:
            async with database.transaction() as session:
                first, second = Region(name="A", slug="a"), Region(name="B", slug="b")
                session.add(first)
                session.add(second)
                await session.flush()
                branch = Region(name="Branch", slug="branch", parent_id=first.id)
                session.add(branch)
            return first.id, second.id, branch.id
        finally:
            await database.close()

    first, second, branch = asyncio.run(seed())
    url = postgres_endpoints.pooled if pooled else postgres_endpoints.direct

    def write(moving: bool) -> None:
        """Issue structural changes through separate real connections and transactions."""
        with psycopg.connect(
            url, autocommit=True, prepare_threshold=None if pooled else 5
        ) as connection:
            connection.execute("SET statement_timeout = '10s'")
            for index in range(10):
                if moving:
                    connection.execute(
                        "UPDATE regions SET parent_id=%s WHERE id=%s",
                        (first if index % 2 else second, branch),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO regions
                            (id, name, slug, description, parent_id, created, last_updated)
                        VALUES (%s, %s, %s, '', %s, now(), now())
                    """,
                        (uuid4(), f"Child {index}", f"child-{index}", branch),
                    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(write, [True, False]))
    with psycopg.connect(postgres_endpoints.direct) as connection:
        mismatch = connection.execute("""
            WITH RECURSIVE expected AS (
                SELECT id, ARRAY[id] AS path FROM regions WHERE parent_id IS NULL
                UNION ALL
                SELECT child.id, parent.path || child.id FROM regions child
                JOIN expected parent ON child.parent_id = parent.id
            )
            SELECT count(*) FROM regions r LEFT JOIN expected e ON e.id=r.id
            WHERE e.id IS NULL OR e.path <> r.path
        """).fetchone()[0]
        assert mismatch == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM regions WHERE parent_id=%s", (branch,)
            ).fetchone()[0]
            == 10
        )


def test_trigger_restore_and_private_paths(auth_settings: Settings) -> None:
    """Trigger DDL survives an empty search path; ordinary writes cannot corrupt ancestry."""
    names = ["lock_region_tree", "set_region_path", "cascade_region_path", "guard_region_path"]
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute("SET LOCAL search_path = ''")
        functions = connection.execute(
            """
            SELECT pg_get_functiondef(p.oid) FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.proname = ANY(%s)
        """,
            (names,),
        ).fetchall()
        triggers = connection.execute("""
            SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger
            WHERE tgrelid='public.regions'::regclass AND NOT tgisinternal
        """).fetchall()
        assert len(functions) == len(triggers) == 4
        for name, _ in triggers:
            connection.execute(
                psycopg.sql.SQL("DROP TRIGGER {} ON public.regions").format(
                    psycopg.sql.Identifier(name)
                )
            )
        for name in names:
            connection.execute(
                psycopg.sql.SQL("DROP FUNCTION public.{}()").format(psycopg.sql.Identifier(name))
            )
        for (definition,) in functions:
            connection.execute(definition)
        for _, definition in triggers:
            connection.execute(definition)
        identity = uuid4()
        connection.execute(
            """
            INSERT INTO public.regions(id, name, slug, description, created, last_updated)
            VALUES (%s, 'Restored', 'restored', '', now(), now())
        """,
            (identity,),
        )
        assert connection.execute(
            "SELECT path FROM public.regions WHERE id=%s", (identity,)
        ).fetchone()[0] == [identity]
    for update in (
        "UPDATE regions SET path='{}'::uuid[] WHERE id=%s",
        "UPDATE regions SET id=gen_random_uuid() WHERE id=%s",
    ):
        with (
            psycopg.connect(auth_settings.database_url.get_secret_value()) as connection,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            connection.execute(update, (identity,))


@pytest.mark.parametrize("pooled", [False, True])
def test_api_service_reparent_lock_order(
    auth_settings: Settings, postgres_endpoints: PostgresEndpoints, pooled: bool
) -> None:
    """API object locking cannot invert the tree lock during crossing reparent requests."""

    async def scenario() -> None:
        settings = (
            auth_settings.model_copy(
                update={
                    "database_url": SecretStr(postgres_endpoints.pooled),
                    "database_mode": "pgbouncer",
                }
            )
            if pooled
            else auth_settings
        )
        database = Database(settings)
        try:
            owner = await LocalPasswordProvider(database).bootstrap(credentials())
            sessions = SessionService(database)
            issued = await sessions.issue(owner)
            async with database.transaction() as session:
                first, second = Region(name="A", slug="a"), Region(name="B", slug="b")
                session.add(first)
                session.add(second)

            async def move(identity: UUID, parent: UUID) -> Region:
                """Run the same domain service, principal validation and transaction as HTTP."""
                async with database.session() as session:
                    service = RegionService(Region, session, AuthorizationService(sessions))
                    return await service.save(
                        issued.principal, {"parent_id": parent}, identity=identity
                    )

            results = await asyncio.wait_for(
                asyncio.gather(
                    move(first.id, second.id), move(second.id, first.id), return_exceptions=True
                ),
                timeout=15,
            )
            assert sum(isinstance(result, Region) for result in results) == 1
            conflicts = [result for result in results if isinstance(result, DefinitionError)]
            assert len(conflicts) == 1 and conflicts[0].status == 409
        finally:
            await database.close()

    asyncio.run(scenario())


def test_async_registry_projection_preserves_stored_overrides(auth_settings: Settings) -> None:
    """Await real typed handlers to verify projection after writes and inherited flag reads.

    This additionally exercises the async continuation outside the browser transport
    thread: returned DTOs must preserve identity/audit fields without persisting a
    copied ancestor flag or leaking the private ancestry column.
    """

    async def scenario() -> None:
        from tinypods_api.clusters import routes
        from tinypods_api.clusters.contracts import (
            ClusterPatch,
            ClusterQuery,
            ClusterWrite,
            RegionPatch,
            RegionQuery,
            RegionWrite,
        )
        from tinypods_core.applications.service import DefinitionService
        from tinypods_core.clusters import Cluster

        database = Database(auth_settings)
        try:
            owner = await LocalPasswordProvider(database).bootstrap(credentials())
            sessions = SessionService(database)
            principal = (await sessions.issue(owner)).principal
            authorization = AuthorizationService(sessions)
            async with database.session() as session:
                root = await routes.create_region(
                    RegionWrite(name="India", slug="india", flag=FLAG),
                    RegionService(Region, session, authorization),
                    principal,
                )
            async with database.session() as session:
                child = await routes.create_region(
                    RegionWrite(name="Gurugram", slug="gurugram", parent_id=root.id),
                    RegionService(Region, session, authorization),
                    principal,
                )
            async with database.session() as session:
                service = RegionService(Region, session, authorization)
                record = await service.get(principal, child.id)
                projected = await service.resolve_flags([record])
                assert record.flag is None and projected[0].flag == FLAG
                assert record is not projected[0]
            async with database.session() as session:
                page = await routes.list_region(
                    RegionQuery(), RegionService(Region, session, authorization), principal
                )
                assert page.total == 2 and {item.flag for item in page.items} == {FLAG}
            async with database.session() as session:
                changed = await routes.update_region(
                    child.id,
                    RegionPatch(flag=None),
                    RegionService(Region, session, authorization),
                    principal,
                )
                assert changed.flag == FLAG and "path" not in changed.model_dump()
            async with database.session() as session:
                cluster = await routes.create_cluster(
                    ClusterWrite(name="Cluster 1", slug="cluster-1", region_id=child.id),
                    DefinitionService(Cluster, session, authorization),
                    principal,
                )
                assert cluster.created and cluster.last_updated and cluster.region_id == child.id
            async with database.session() as session:
                actual = await routes.retrieve_cluster(
                    cluster.id, DefinitionService(Cluster, session, authorization), principal
                )
                assert actual == cluster and "path" not in actual.model_dump()
            async with database.session() as session:
                changed_cluster = await routes.replace_cluster(
                    cluster.id,
                    ClusterWrite(name="Cluster 2", slug="cluster-2", region_id=child.id),
                    DefinitionService(Cluster, session, authorization),
                    principal,
                )
                assert changed_cluster.id == cluster.id and changed_cluster.name == "Cluster 2"
            async with database.session() as session:
                changed_cluster = await routes.update_cluster(
                    cluster.id,
                    ClusterPatch(description="Raspberry Pi"),
                    DefinitionService(Cluster, session, authorization),
                    principal,
                )
                assert changed_cluster.description == "Raspberry Pi"
            async with database.session() as session:
                page_clusters = await routes.list_cluster(
                    ClusterQuery(region_id=child.id),
                    DefinitionService(Cluster, session, authorization),
                    principal,
                )
                assert page_clusters.total == 1 and page_clusters.items[0].id == cluster.id
        finally:
            await database.close()

    asyncio.run(scenario())
