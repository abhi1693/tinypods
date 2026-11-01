"""Repository models, sync, CLI and admin API on isolated PostgreSQL/Valkey."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from conftest import PostgresEndpoints
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from tinypods_api.main import create_app
from tinypods_cli.main import app as cli
from tinypods_core.applications import Application, Release, SourceType
from tinypods_core.applications.service import DefinitionError, DefinitionService
from tinypods_core.audit import AuditRecord
from tinypods_core.auth import AuthError, Principal
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database, MigrationManager, migration_engine
from tinypods_core.repositories import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.service import RepositorySync
from tinypods_core.repositories.sources import Observation, SourceError, SourceSnapshot
from tinypods_core.settings import Settings
from typer.testing import CliRunner

BASE = "/api/v1"
DIGEST = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def test_populated_repository_migration_and_database_protection(
    postgres_endpoints: PostgresEndpoints,
) -> None:
    """Backfill real legacy rows, preserve pins and enforce immutable/FK identities."""
    settings = Settings(database_url=SecretStr(postgres_endpoints.direct))
    manager = MigrationManager(settings, ROOT / "alembic.ini")
    with psycopg.connect(postgres_endpoints.direct, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    command.upgrade(manager.config(), "0004_applications")
    ids = [uuid4(), uuid4()]
    releases = [uuid4(), uuid4()]
    with psycopg.connect(postgres_endpoints.direct) as connection:
        for index in range(2):
            connection.execute(
                "INSERT INTO applications "
                "(id,created,last_updated,name,slug,description,status) "
                "VALUES (%s,now(),now(),%s,%s,'','experimental')",
                (ids[index], "App " + str(index), "app-" + str(index)),
            )
            connection.execute(
                "INSERT INTO releases (id,created,last_updated,application_id,chart_version,"
                "source_type,source_location,chart_name,digest,configuration_fields) "
                "VALUES (%s,now(),now(),%s,'1.0.0','helm_repository',"
                "'https://charts.example.test','example',%s,'[]')",
                (releases[index], ids[index], "sha256:" + ("a" if index == 0 else "b") * 64),
            )
    manager.upgrade()
    manager.check()
    engine = migration_engine(settings)
    try:
        with Session(engine) as session:
            assert len(session.exec(select(ChartRepository)).all()) == 1
            assert len(session.exec(select(Chart)).all()) == 1
            versions = session.exec(select(ChartVersion)).all()
            assert len(versions) == 2 and {v.digest for v in versions} == {
                DIGEST,
                "sha256:" + "b" * 64,
            }
            pinned = session.get(Release, releases[0])
            assert session.get(ChartVersion, pinned.chart_version_id).digest == DIGEST
            repository = session.exec(select(ChartRepository)).one()
            repo_id = repository.id
            assert repository.source_type is SourceType.HELM_REPOSITORY
            assert repository.last_synced_at is None
            assert not session.exec(select(Chart)).one().available
        assert "source_location" not in {
            column["name"] for column in inspect(engine).get_columns("releases")
        }
        for sql, parameters in (
            ("UPDATE chart_versions SET digest=:digest", {"digest": "sha256:" + "c" * 64}),
            ("UPDATE charts SET name='moved'", {}),
            ("DELETE FROM chart_repositories WHERE id=:id", {"id": repo_id}),
            ("UPDATE releases SET chart_version_id=:missing", {"missing": uuid4()}),
        ):
            with engine.begin() as connection, pytest.raises(IntegrityError):
                connection.execute(text(sql), parameters)
        command.downgrade(manager.config(), "0004_applications")
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id,source_location,chart_name,chart_version,digest "
                    "FROM releases ORDER BY digest"
                )
            ).all()
            assert [row.digest for row in rows] == [DIGEST, "sha256:" + "b" * 64]
            assert {row.id for row in rows} == set(releases)
            assert all(
                row.source_location == "https://charts.example.test"
                and row.chart_name == "example"
                and row.chart_version == "1.0.0"
                for row in rows
            )
        manager.upgrade()
        manager.check()
    finally:
        engine.dispose()


def test_repository_api_cli_and_cascade_rules(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use real auth/DB, deterministic source metadata and the actual CLI entry point."""

    async def bootstrap() -> None:
        """Initialize only the disposable test installation."""
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(bootstrap())
    observations = [Observation(chart="example", version="1.0.0", digest=DIGEST)]
    failed = False
    throttled = False

    async def fetch(
        self: RepositoryClient, source_type: str, url: str, known_charts: tuple[str, ...] = ()
    ) -> SourceSnapshot:
        """Replace external I/O only; persistence and policy remain production implementations."""
        if failed:
            raise SourceError(502, "source_unavailable")
        if throttled:
            raise SourceError(429, "source_rate_limited")
        return SourceSnapshot(tuple(observations), True)

    monkeypatch.setattr(RepositoryClient, "fetch", fetch)
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        assert client.get(BASE + "/chart-repositories").status_code == 401
        assert (
            client.post(
                BASE + "/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": client.cookies["__Host-tinypods-csrf"]}
        response = client.post(
            BASE + "/chart-repositories",
            json={"name": "Example", "url": "https://charts.example.test/"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        repository = response.json()
        identity = repository["id"]
        assert (
            repository["source_type"] == "helm_repository"
            and repository["url"] == "https://charts.example.test"
        )
        assert repository["last_synced_at"] is None
        sync = BASE + "/chart-repositories/" + identity + "/sync"
        assert client.post(sync).status_code == 403
        assert client.post(sync, headers=headers).json()["versions"] == 1
        charts = client.get(
            BASE + "/charts?repository_id=" + identity + "&available=true&q=example&ordering=-name"
        ).json()
        chart = charts["items"][0]
        version = client.get(
            BASE
            + "/chart-versions?chart_id="
            + chart["id"]
            + "&available=true&q=1.0&ordering=-version"
        ).json()["items"][0]
        assert version["digest"] == DIGEST
        for path in (
            "/chart-repositories/" + identity,
            "/charts/" + chart["id"],
            "/chart-versions/" + version["id"],
        ):
            assert client.get(BASE + path).status_code == 200
        assert (
            client.get(
                BASE + "/chart-repositories?source_type=helm_repository&enabled=true"
                "&q=Example&ordering=-name"
            ).json()["total"]
            == 1
        )
        assert client.get(BASE + "/chart-versions?ordering=created").json()["total"] == 1
        assert (
            client.patch(
                BASE + "/chart-repositories/" + identity, json={"name": "Renamed"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.put(
                BASE + "/chart-repositories/" + identity,
                json={"name": "Replaced", "url": "https://charts.example.test"},
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                BASE + "/chart-repositories/" + identity,
                json={"source_type": "oci_registry"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                BASE + "/chart-repositories",
                json={"name": "bad", "url": "https://user:secret@charts.example.test"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                BASE + "/chart-repositories",
                json={"name": "bad", "url": "https://charts.example.test/index.yaml"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.patch(
                BASE + "/charts/" + chart["id"], json={"name": "changed"}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.put(
                BASE + "/charts/" + chart["id"],
                json={"repository_id": identity, "name": "changed"},
                headers=headers,
            ).status_code
            == 409
        )
        extra = client.post(
            BASE + "/charts",
            json={"repository_id": identity, "name": "registered"},
            headers=headers,
        )
        assert extra.status_code == 201
        assert (
            client.patch(
                BASE + "/charts/" + extra.json()["id"], json={"name": "corrected"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.put(
                BASE + "/charts/" + extra.json()["id"],
                json={"repository_id": identity, "name": "corrected"},
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.delete(BASE + "/charts/" + extra.json()["id"], headers=headers).status_code
            == 204
        )
        app = client.post(
            BASE + "/applications",
            json={"name": "Uses chart", "slug": "uses-chart"},
            headers=headers,
        ).json()
        release = client.post(
            BASE + "/releases",
            json={"application_id": app["id"], "chart_version_id": version["id"]},
            headers=headers,
        )
        assert release.status_code == 201, release.text
        # A changed upstream tag is a distinct observation; an existing pin is unchanged.
        observations[0] = Observation(chart="example", version="1.0.0", digest="sha256:" + "b" * 64)
        assert client.post(sync, headers=headers).json()["versions"] == 1
        versions = client.get(BASE + "/chart-versions?chart_id=" + chart["id"]).json()["items"]
        assert len(versions) == 2 and sum(v["available"] for v in versions) == 1
        assert (
            client.get(BASE + "/releases/" + release.json()["id"]).json()["chart_version_id"]
            == version["id"]
        )
        assert (
            client.delete(BASE + "/chart-repositories/" + identity, headers=headers).status_code
            == 409
        )
        assert client.delete(BASE + "/charts/" + chart["id"], headers=headers).status_code == 409
        assert client.get(BASE + "/chart-versions?chart_id=" + chart["id"]).json()["total"] == 2
        failed = True
        error = client.post(sync, headers=headers)
        assert error.status_code == 502 and error.json()["code"] == "source_unavailable"
        assert client.get(BASE + "/chart-versions?available=true").json()["total"] == 1
        failed = False
        throttled = True
        limited = client.post(sync, headers=headers)
        assert limited.status_code == 429 and limited.json()["code"] == "source_rate_limited"
        throttled = False
        env = {
            "TINYPODS_DATABASE_URL": auth_settings.database_url.get_secret_value(),
            "TINYPODS_REPOSITORY_CREDENTIALS": "{}",
        }
        outcome = CliRunner().invoke(cli, ["repositories", "sync", identity], env=env)
        assert outcome.exit_code == 0, outcome.output + str(outcome.exception)
        assert json.loads(outcome.stdout)["repositories"][0]["versions"] == 1
        all_sources = CliRunner().invoke(cli, ["repositories", "sync"], env=env)
        assert all_sources.exit_code == 0
        assert (
            client.patch(
                BASE + "/chart-repositories/" + identity, json={"enabled": False}, headers=headers
            ).status_code
            == 200
        )
        disabled = CliRunner().invoke(cli, ["repositories", "sync", identity], env=env)
        assert (
            disabled.exit_code == 1
            and json.loads(disabled.stdout)["repositories"][0]["code"] == "http_409"
        )
        assert CliRunner().invoke(cli, ["repositories", "sync"], env=env).exit_code == 0
        missing = CliRunner().invoke(cli, ["repositories", "sync", str(uuid4())], env=env)
        assert missing.exit_code == 1
        assert (
            client.delete(BASE + "/releases/" + release.json()["id"], headers=headers).status_code
            == 204
        )
        assert (
            client.delete(BASE + "/chart-repositories/" + identity, headers=headers).status_code
            == 204
        )
        assert client.get(BASE + "/charts?repository_id=" + identity).json()["total"] == 0
        assert client.get(BASE + "/chart-versions").json()["total"] == 0
        assert (
            client.delete(BASE + "/chart-repositories/" + identity, headers=headers).status_code
            == 404
        )


def test_sync_generation_rollback_and_operator_audit(auth_settings: Settings) -> None:
    """Revalidate permissions, reject stale fetches, and audit cascade children atomically."""

    class Validator:
        """Trusted test credential boundary, independent of HTTP/cookie mechanics."""

        async def validate_principal(self, principal: Principal) -> Principal:
            """Return the supplied normalized test identity."""
            return principal

    owner = Principal(uuid4(), "owner", True, "test")
    reader = Principal(uuid4(), "reader", False, "test")
    authorization = AuthorizationService(Validator())

    async def scenario() -> None:
        """Exercise direct services so transaction safety is verified without routing."""
        database = Database(auth_settings)
        try:
            async with database.session() as session:
                repository = await DefinitionService(ChartRepository, session, authorization).save(
                    owner, {"name": "Direct", "url": "https://charts.example.test"}
                )

            class Source:
                """Run a controlled source side effect while no read connection is held."""

                change = False
                remove = False
                empty = False

                async def fetch(
                    self, source_type: str, url: str, known_charts: tuple[str, ...]
                ) -> SourceSnapshot:
                    """Return known metadata or model a concurrent repository edit/deletion."""
                    if self.change:
                        async with database.session() as session:
                            await DefinitionService(ChartRepository, session, authorization).save(
                                owner, {"name": "Concurrent edit"}, identity=repository.id
                            )
                    if self.remove:
                        async with database.session() as session:
                            await DefinitionService(ChartRepository, session, authorization).delete(
                                owner, repository.id
                            )
                    return (
                        SourceSnapshot((), True)
                        if self.empty
                        else SourceSnapshot(
                            (Observation(chart="example", version="1.0.0", digest=DIGEST),), True
                        )
                    )

            source = Source()
            service = RepositorySync(database, authorization, source)
            with pytest.raises(AuthError):
                await service.run(reader, repository.id)
            with pytest.raises(DefinitionError):
                await service.run(owner, uuid4())
            source.change = True
            with pytest.raises(DefinitionError) as stale:
                await service.run(owner, repository.id)
            assert stale.value.status == 409
            source.change = False
            assert (await service.run(owner, repository.id)).versions == 1
            assert (await service.run(owner, repository.id)).versions == 1
            async with database.session() as session:
                versions = (await session.exec(select(ChartVersion))).all()
                assert len(versions) == 1
                chart = (await session.exec(select(Chart))).one()
                app = Application(name="Pinned", slug="pinned")
                session.add(app)
                await session.commit()
            async with database.session() as session:
                release = await DefinitionService(Release, session, authorization).save(
                    owner, {"application_id": app.id, "chart_version_id": versions[0].id}
                )
            async with database.session() as session:
                with pytest.raises(DefinitionError):
                    await DefinitionService(ChartRepository, session, authorization).delete(
                        owner, repository.id
                    )
                assert not session.in_transaction()
            source.empty = True
            # Empty valid snapshots hide availability, but preserve referenced rows.
            assert (await service.run(owner, repository.id)).versions == 0
            async with database.session() as session:
                assert not (await session.get(ChartVersion, versions[0].id)).available
                await session.rollback()
            async with database.session() as session:
                await DefinitionService(Release, session, authorization).delete(owner, release.id)
            async with database.session() as session:
                await DefinitionService(ChartRepository, session, authorization).delete(
                    owner, repository.id
                )
            async with database.session() as session:
                assert not (await session.exec(select(Chart))).all()
                assert not (await session.exec(select(ChartVersion))).all()
                deletions = (
                    await session.exec(select(AuditRecord).where(AuditRecord.action == "delete"))
                ).all()
                assert {row.changed_object_id for row in deletions} >= {
                    str(repository.id),
                    str(chart.id),
                    str(versions[0].id),
                }
            async with database.session() as session:
                repository = await DefinitionService(ChartRepository, session, authorization).save(
                    owner, {"name": "Removed during fetch", "url": "https://other.example.test"}
                )
            source = Source()
            source.remove = True
            service = RepositorySync(database, authorization, source)
            with pytest.raises(DefinitionError) as gone:
                await service.run(owner, repository.id)
            assert gone.value.status == 404
        finally:
            await database.close()

    asyncio.run(scenario())


def test_repository_service_and_dto_boundaries(auth_settings: Settings) -> None:
    """Verify service validation and page DTO conversion independently of HTTP routing."""
    from types import SimpleNamespace

    from fastapi import HTTPException
    from starlette.requests import Request
    from tinypods_api.repositories.contracts import ChartQuery, RepositoryQuery, VersionQuery
    from tinypods_api.repositories.dependencies import sync
    from tinypods_api.repositories.routes import list_chart, list_repository, list_version

    with pytest.raises(HTTPException) as unavailable:
        sync(
            Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(database=None))}),
            None,
        )
    assert unavailable.value.status_code == 503

    class Validator:
        """Trusted non-browser authentication boundary for direct service regressions."""

        async def validate_principal(self, principal: Principal) -> Principal:
            """Return the command-owned test identity."""
            return principal

    async def scenario() -> None:
        """Check rollback, safe metadata serialization and audited chart cascade deletion."""
        database = Database(auth_settings)
        principal = Principal(uuid4(), "test", True, "test")
        authorization = AuthorizationService(Validator())
        try:
            async with database.session() as session:
                with pytest.raises(DefinitionError) as rejected:
                    await DefinitionService(ChartRepository, session, authorization).save(
                        principal,
                        {
                            "name": "Invalid",
                            "source_type": "oci_registry",
                            "url": "https://registry.example.test",
                        },
                    )
                assert rejected.value.status == 422 and not session.in_transaction()
            async with database.session() as session:
                repository = await DefinitionService(ChartRepository, session, authorization).save(
                    principal, {"name": "Direct", "url": "https://charts.example.test"}
                )
            async with database.transaction() as session:
                chart = Chart(repository_id=repository.id, name="example", available=True)
                session.add(chart)
                await session.flush()
                version = ChartVersion(
                    chart_id=chart.id, version="1.0.0", digest=DIGEST, available=True
                )
                session.add(version)
            for model, handler, query in (
                (ChartRepository, list_repository, RepositoryQuery()),
                (Chart, list_chart, ChartQuery()),
                (ChartVersion, list_version, VersionQuery()),
            ):
                async with database.session() as session:
                    result = await handler(
                        query, DefinitionService(model, session, authorization), principal
                    )
                    assert result.total == 1 and len(result.items) == 1
            async with database.session() as session:
                await DefinitionService(Chart, session, authorization).delete(principal, chart.id)
            async with database.session() as session:
                assert await session.get(ChartVersion, version.id) is None
                assert await session.get(ChartRepository, repository.id) is not None
        finally:
            await database.close()

    asyncio.run(scenario())
