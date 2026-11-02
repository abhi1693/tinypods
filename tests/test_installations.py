"""Real PostgreSQL installation snapshots, isolation and confidential-version bindings."""

import asyncio
import json
from collections.abc import Awaitable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from secret_fixtures import key_settings
from test_auth import auth_settings as auth_settings
from test_release_publication import CatalogAPI
from test_release_publication import catalog_api as catalog_api
from tinypods_core.settings import Settings

BASE = "/api/v1"
PAYLOAD = "fixture-confidential-application-value"


@dataclass
class InstallationAPI:
    """Carry isolated authenticated API and immutable catalog inputs."""

    catalog: CatalogAPI
    installation: str
    cluster: str

    @property
    def client(self) -> TestClient:
        """Return the current owner session."""
        return self.catalog.client

    @property
    def path(self) -> str:
        """Return the saved intended installation resource."""
        return BASE + "/installations/" + self.installation

    def revision(self, **changes: object) -> dict[str, object]:
        """Build an independent configuration save from explicit user choices."""
        return {
            "release_id": self.catalog.release,
            "overrides": {"replicas": 2},
            **changes,
        }


@pytest.fixture
def installation_api(catalog_api: CatalogAPI, auth_settings: Settings) -> Iterator[InstallationAPI]:
    """Publish a real chart definition and reserve an isolated target; no Kubernetes calls."""
    client = catalog_api.client
    client.headers.update(catalog_api.headers)
    client.app.state.settings = key_settings(auth_settings)  # type: ignore[union-attr]
    fields = [
        {"label": "Replicas", "path": "replicas", "type": "number", "set_by": "user"},
        {"label": "Password", "path": "password", "type": "password", "set_by": "user"},
        {"label": "Existing secret", "path": "existingSecret", "type": "text", "set_by": "user"},
    ]
    assert client.patch(catalog_api.path, json={"configuration_fields": fields}).status_code == 200
    published = client.post(catalog_api.path + "/publication")
    assert published.status_code == 200, published.text
    region = client.post(BASE + "/regions", json={"name": "Test", "slug": "test"})
    assert region.status_code == 201, region.text
    cluster = client.post(
        BASE + "/clusters", json={"name": "Test", "slug": "test", "region_id": region.json()["id"]}
    )
    assert cluster.status_code == 201, cluster.text
    created = client.post(
        BASE + "/installations",
        json={"name": "First", "cluster_id": cluster.json()["id"], "namespace": "test"},
    )
    assert created.status_code == 201, created.text
    yield InstallationAPI(catalog_api, created.json()["id"], cluster.json()["id"])


def test_independent_history_and_read_contract(installation_api: InstallationAPI) -> None:
    """Independent saved settings never advance deployed revision or depend on upstream reads."""
    api = installation_api
    client = api.client
    assert client.get(api.path).json()["revision"] is None
    first = client.post(api.path + "/revisions", json=api.revision())
    assert first.status_code == 201, first.text
    first_data = first.json()
    assert first_data["values"]["replicas"] == 2
    assert first_data["overrides"] == {"replicas": 2}
    assert client.get(api.path).json()["revision"] is None
    second = client.post(
        BASE + "/installations",
        json={"name": "Second", "cluster_id": api.cluster, "namespace": "test"},
    )
    second_path = BASE + "/installations/" + second.json()["id"]
    other = client.post(second_path + "/revisions", json=api.revision(overrides={"replicas": 7}))
    assert other.status_code == 201 and other.json()["values"]["replicas"] == 7
    assert client.get(second_path + "/revisions/" + first_data["id"]).status_code == 404
    assert client.patch(api.path, json={"name": "Renamed"}).json()["name"] == "Renamed"
    for field, value in (
        ("revision_id", first_data["id"]),
        ("owner_id", str(uuid4())),
        ("namespace", "elsewhere"),
        ("cluster_id", str(uuid4())),
    ):
        assert client.patch(api.path, json={field: value}).status_code == 422
    assert (
        client.get(BASE + "/installations", params={"limit": 1, "offset": 1}).json()["total"] == 2
    )
    second_revision = client.post(
        api.path + "/revisions",
        json=api.revision(overrides={"replicas": 3}),
    )
    assert second_revision.status_code == 201, second_revision.text
    history = client.get(api.path + "/revisions", params={"limit": 1, "offset": 1}).json()
    assert history["total"] == 2 and history["items"][0]["id"] == first_data["id"]
    newest = client.get(api.path + "/revisions", params={"limit": 1}).json()
    assert newest["items"][0]["id"] == second_revision.json()["id"]
    assert client.patch(api.catalog.app_path, json={"name": "Catalog renamed"}).status_code == 200
    assert (
        client.post(api.catalog.path + "/revocation", json={"reason": "Fixture"}).status_code == 200
    )
    read = client.get(api.path + "/revisions/" + first_data["id"]).json()
    for key in (
        "id",
        "created",
        "last_updated",
        "values",
        "overrides",
        "secrets",
        "kubernetes_secrets",
    ):
        assert read[key] == first_data[key]
    assert (
        client.post(api.path + "/validation", json={**api.revision(), "unknown": True}).status_code
        == 422
    )
    assert client.put(api.path + "/revisions/" + first_data["id"], json={}).status_code == 405
    assert client.delete(api.path + "/revisions/" + first_data["id"]).status_code == 405
    missing = BASE + "/installations/" + str(uuid4())
    assert client.get(missing).status_code == 404
    assert client.patch(missing, json={"name": "Missing"}).status_code == 404
    assert client.get(missing + "/revisions").status_code == 404


def test_validation_and_scoped_secrets(
    installation_api: InstallationAPI, auth_settings: Settings
) -> None:
    """Pin exact versions, reject literals/cross-scope/revocation, and keep payloads out of JSON."""
    api = installation_api
    client = api.client
    initial = {"release_id": api.catalog.release, "overrides": {"replicas": 4}}
    assert client.post(api.path + "/validation", json=initial).json() == {
        "valid": True,
        "issues": [],
    }
    for overrides, code in (
        ({"unknown": PAYLOAD}, "unknown_configuration_path"),
        ({"password": PAYLOAD}, "confidential_literal"),
    ):
        response = client.post(api.path + "/validation", json={**initial, "overrides": overrides})
        assert response.status_code == 200 and not response.json()["valid"]
        assert response.json()["issues"][0]["code"] == code and PAYLOAD not in response.text
    assert client.get(api.path + "/revisions").json()["total"] == 0
    secret = client.post(api.path + "/secrets", json={"name": "Password", "payload": PAYLOAD})
    assert secret.status_code == 201, secret.text
    secret_id = secret.json()["secret"]["id"]
    versions_path = api.path + "/secrets/" + secret_id + "/versions"
    one = client.get(versions_path).json()["items"][0]
    two = client.post(versions_path, json={"payload": PAYLOAD + "-two"})
    assert two.status_code == 201 and two.json()["version"] == 2
    assert client.get(versions_path + "/" + one["id"]).json() == one
    assert client.get(api.path + "/secrets").json()["total"] == 1
    bindings = [{"version_id": one["id"], "target": "password", "use": "value"}]
    kube = [
        {"namespace": "test", "name": "existing", "key": "password", "target": "existingSecret"}
    ]
    saved = client.post(
        api.path + "/revisions", json=api.revision(secrets=bindings, kubernetes_secrets=kube)
    )
    assert saved.status_code == 201, saved.text
    assert "password" not in saved.json()["values"]
    assert saved.json()["values"]["existingSecret"] == "existing"
    assert saved.json()["secrets"][0]["version"]["id"] == one["id"]
    assert PAYLOAD not in saved.text
    cross = client.post(
        BASE + "/installations",
        json={"name": "Other", "cluster_id": api.cluster, "namespace": "test"},
    ).json()["id"]
    assert (
        client.post(
            BASE + "/installations/" + cross + "/revisions", json=api.revision(secrets=bindings)
        ).status_code
        == 409
    )
    assert (
        client.get(
            BASE + "/installations/" + cross + "/secrets/" + secret_id + "/versions"
        ).status_code
        == 404
    )
    assert client.get(versions_path + "/" + str(uuid4())).status_code == 404
    assert (
        client.post(
            api.path + "/validation",
            json={**initial, "kubernetes_secrets": [{**kube[0], "namespace": "other"}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            api.path + "/validation", json={**initial, "secrets": bindings + bindings}
        ).status_code
        == 422
    )
    assert client.post(versions_path + "/" + one["id"] + "/revocation").status_code == 200
    assert client.post(versions_path + "/" + one["id"] + "/revocation").status_code == 200
    assert (
        client.post(
            api.path + "/revisions",
            json=api.revision(secrets=bindings),
        ).status_code
        == 409
    )
    assert client.get(api.path + "/revisions/" + saved.json()["id"]).status_code == 200
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        for table in ("installation_revisions", "audit_records"):
            rows = connection.execute("SELECT row_to_json(t)::text FROM " + table + " t").fetchall()
            assert PAYLOAD not in json.dumps(rows)


def test_database_history_guards(
    installation_api: InstallationAPI, auth_settings: Settings
) -> None:
    """Direct writes cannot rewrite history, change targets or cross installation pointers."""
    api = installation_api
    first = api.client.post(api.path + "/revisions", json=api.revision()).json()
    other = api.client.post(
        BASE + "/installations",
        json={"name": "Other", "cluster_id": api.cluster, "namespace": "test"},
    ).json()
    statements = [
        ("UPDATE installation_revisions SET values = '{}' WHERE id = %s", (UUID(first["id"]),)),
        ("DELETE FROM installation_revisions WHERE id = %s", (UUID(first["id"]),)),
        (
            "UPDATE application_installations SET namespace = 'other' WHERE id = %s",
            (UUID(api.installation),),
        ),
        (
            "UPDATE application_installations SET revision_id = %s WHERE id = %s",
            (UUID(first["id"]), UUID(other["id"])),
        ),
        ("DELETE FROM application_installations WHERE id = %s", (UUID(api.installation),)),
        ("DELETE FROM releases WHERE id = %s", (UUID(api.catalog.release),)),
    ]
    for statement, params in statements:
        with (
            psycopg.connect(auth_settings.database_url.get_secret_value()) as connection,
            pytest.raises(psycopg.IntegrityError),
            connection.transaction(),
        ):
            connection.execute(statement, params)
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            "UPDATE application_installations SET revision_id = %s WHERE id = %s",
            (UUID(first["id"]), UUID(api.installation)),
        )
    assert api.client.get(api.path).json()["revision"]["id"] == first["id"]


def test_concurrent_independent_history(
    installation_api: InstallationAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent valid saves both append independent immutable history."""
    import threading

    from tinypods_core.applications.installation_contracts import (
        KubernetesSecretBinding,
        SecretBinding,
    )
    from tinypods_core.applications.installation_service import InstallationService
    from tinypods_core.applications.installation_validation import InstallationConfiguration
    from tinypods_core.applications.validation import ReleaseDraft
    from tinypods_core.auth import Principal
    from tinypods_core.json_types import JsonRecord

    api = installation_api
    original = InstallationService.prepare
    barrier = threading.Barrier(2)

    async def checked(
        self: InstallationService,
        principal: Principal,
        installation_id: UUID,
        release_id: UUID,
        overrides: JsonRecord,
        secrets: list[SecretBinding],
        kubernetes_secrets: list[KubernetesSecretBinding],
    ) -> tuple[ReleaseDraft, InstallationConfiguration]:
        result = await original(
            self, principal, installation_id, release_id, overrides, secrets, kubernetes_secrets
        )
        await asyncio.to_thread(barrier.wait, 10)
        return result

    monkeypatch.setattr(InstallationService, "prepare", checked)
    # Real bounded validation runs serially; the barrier coordinates only the
    # final competing PostgreSQL transactions after external work has completed.
    from collections.abc import AsyncIterator

    # Admission is covered separately; allow both prepared requests to reach
    # the real database transaction boundary together.
    from contextlib import asynccontextmanager

    from tinypods_core.applications.installation_validation import InstallationValidator

    @asynccontextmanager
    async def admission(self: InstallationValidator) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(InstallationValidator, "admission", admission)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda index: api.client.post(
                    api.path + "/revisions", json=api.revision(overrides={"replicas": index + 2})
                ),
                range(2),
            )
        )
    assert [response.status_code for response in responses] == [201, 201]
    history = api.client.get(api.path + "/revisions").json()
    assert history["total"] == 2
    assert len({item["id"] for item in history["items"]}) == 2
    assert {item["values"]["replicas"] for item in history["items"]} == {2, 3}
    assert api.client.get(api.path).json()["revision"] is None


def test_owner_isolation(installation_api: InstallationAPI, auth_settings: Settings) -> None:
    """A second authenticated user cannot read another owner's installation or secrets."""
    from test_release_publication import ordinary_client

    api = installation_api
    with ordinary_client(auth_settings) as reader:
        assert reader.get(BASE + "/installations").json()["total"] == 0
        for suffix in ("", "/revisions", "/secrets"):
            assert reader.get(api.path + suffix).status_code == 403
    api.client.cookies.clear()
    assert api.client.get(api.path).status_code == 401


def test_application_rotation(installation_api: InstallationAPI, auth_settings: Settings) -> None:
    """Key rotation retains exact logical IDs and application AAD alongside cluster behavior."""
    from tinypods_core.auth import SessionService
    from tinypods_core.auth.authorization import AuthorizationService
    from tinypods_core.db import Database
    from tinypods_core.secrets.applications import ApplicationSecretService
    from tinypods_core.secrets.providers import encryption_provider
    from tinypods_core.secrets.service import SecretService

    api = installation_api
    secret = api.client.post(
        api.path + "/secrets", json={"name": "Password", "payload": PAYLOAD}
    ).json()
    secret_id = secret["secret"]["id"]
    version = api.client.get(api.path + "/secrets/" + secret_id + "/versions").json()["items"][0]
    token = api.client.cookies["__Host-tinypods-session"]
    settings = key_settings(auth_settings, active="new", both=True)

    async def scenario() -> None:
        async with Database(settings) as database:
            sessions = SessionService(database)
            principal = await sessions.authenticate(token)
            authorization = AuthorizationService(sessions)
            provider = encryption_provider(settings)
            async with database.session() as session:
                progress = await SecretService(session, authorization, provider).rotate(principal)
                assert progress.updated == 1 and progress.remaining == 0
            async with database.session() as session:
                value = await ApplicationSecretService(
                    session, authorization, provider
                ).resolve_application_version(
                    principal, UUID(api.installation), UUID(version["id"])
                )
                assert value == PAYLOAD

    asyncio.run(scenario())
    api.client.app.state.settings = settings  # type: ignore[union-attr]
    saved = api.client.post(
        api.path + "/revisions",
        json=api.revision(secrets=[{"version_id": version["id"], "target": "password"}]),
    )
    assert saved.status_code == 201 and PAYLOAD not in saved.text


def test_upstream_and_transaction_failures(
    installation_api: InstallationAPI,
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External failures and exceptions after the INSERT leave no partial revision or audit."""
    from sqlalchemy import event
    from tinypods_core.applications.installations import InstallationRevision
    from tinypods_core.repositories.sources import SourceError
    from tinypods_core.repositories.verification import ChartPin, ChartVerification

    api = installation_api
    original = ChartVerification.download_archive

    async def unavailable(self: ChartVerification, pin: ChartPin) -> bytes:
        raise SourceError(502, "source_unavailable")

    monkeypatch.setattr(ChartVerification, "download_archive", unavailable)
    assert api.client.post(api.path + "/revisions", json=api.revision()).status_code == 502
    assert api.client.get(api.path + "/revisions").json()["total"] == 0

    async def timeout(self: ChartVerification, pin: ChartPin) -> bytes:
        raise TimeoutError

    monkeypatch.setattr(ChartVerification, "download_archive", timeout)
    assert api.client.post(api.path + "/revisions", json=api.revision()).status_code == 504
    monkeypatch.setattr(ChartVerification, "download_archive", original)

    def fail_after_insert(*args: object) -> None:
        raise RuntimeError("controlled failure after insert")

    event.listen(InstallationRevision, "after_insert", fail_after_insert)
    try:
        with pytest.raises(RuntimeError, match="controlled failure"):
            api.client.post(api.path + "/revisions", json=api.revision())
    finally:
        event.remove(InstallationRevision, "after_insert", fail_after_insert)
    assert api.client.get(api.path + "/revisions").json()["total"] == 0
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_records a JOIN object_types t "
            "ON t.id = a.changed_object_type_id WHERE t.model = 'InstallationRevision'"
        ).fetchone() == (0,)
    saved = api.client.post(api.path + "/revisions", json=api.revision())
    assert saved.status_code == 201


def test_mixed_application_and_missing_release(installation_api: InstallationAPI) -> None:
    """The first saved revision fixes application lineage across later release selections."""
    api = installation_api
    first = api.client.post(api.path + "/revisions", json=api.revision())
    assert first.status_code == 201
    app = api.client.post(
        BASE + "/applications", json={"name": "Another", "slug": "another"}
    ).json()
    source = api.client.get(api.catalog.path).json()
    other = api.client.post(
        BASE + "/releases",
        json={
            "application_id": app["id"],
            "chart_version_id": source["chart_version"]["id"],
            "architectures": ["arm64"],
        },
    ).json()
    assert api.client.post(BASE + "/releases/" + other["id"] + "/publication").status_code == 200
    assert (
        api.client.post(
            api.path + "/revisions",
            json=api.revision(release_id=other["id"], overrides={}),
        ).status_code
        == 409
    )
    assert (
        api.client.post(
            api.path + "/revisions",
            json=api.revision(release_id=str(uuid4())),
        ).status_code
        == 404
    )
    assert (
        api.client.post(
            api.path + "/revisions",
            json=api.revision(overrides={"password": PAYLOAD}),
        ).status_code
        == 422
    )
    assert api.client.get(api.path + "/revisions").json()["total"] == 1
    assert (
        api.client.post(api.catalog.path + "/revocation", json={"reason": "Fixture"}).status_code
        == 200
    )
    assert api.client.post(api.path + "/revisions", json=api.revision()).status_code == 409


def test_core_rechecks_and_cancellation(
    installation_api: InstallationAPI,
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check eligibility races, rollback and cancellation against real PostgreSQL."""
    from dataclasses import replace

    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.pool import QueuePool
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tinypods_core.applications.installation_service import InstallationService
    from tinypods_core.applications.installation_validation import InstallationValidator
    from tinypods_core.applications.installations import (
        InstallationRevision,
    )
    from tinypods_core.applications.service import DefinitionError
    from tinypods_core.auth import SessionService
    from tinypods_core.auth.authorization import AuthorizationService
    from tinypods_core.db import Database
    from tinypods_core.repositories.verification import ChartPin, ChartVerification
    from tinypods_core.secrets.providers import encryption_provider

    api = installation_api
    token = api.client.cookies["__Host-tinypods-session"]
    settings = key_settings(auth_settings)

    async def scenario() -> None:
        async with Database(settings) as database:
            sessions = SessionService(database)
            principal = await sessions.authenticate(token)
            authorization = AuthorizationService(sessions)
            charts = ChartVerification(
                database, authorization, api.client.app.state.repository_client
            )  # type: ignore[union-attr]
            service = InstallationService(
                database,
                authorization,
                charts,
                InstallationValidator(),
                encryption_provider(settings),
            )
            identity, release = UUID(api.installation), UUID(api.catalog.release)
            assert (await service.get(principal, identity)).helm_name == "tp-" + identity.hex

            async def error(status: int, action: Awaitable[object]) -> None:
                with pytest.raises(DefinitionError) as caught:
                    await action
                assert caught.value.status == status

            await error(422, service.create(principal, " ", UUID(api.cluster), "test"))
            await error(404, service.create(principal, "Missing", uuid4(), "test"))
            await error(409, service.create(principal, "Invalid", UUID(api.cluster), "INVALID"))
            await error(422, service.rename(principal, identity, " "))
            await error(404, service.prepare(principal, identity, uuid4(), {}, [], []))

            original_pin = charts.lock_pin

            async def changed_pin(session: AsyncSession, version_id: UUID) -> ChartPin:
                return replace(await original_pin(session, version_id), digest="sha256:" + "f" * 64)

            monkeypatch.setattr(charts, "lock_pin", changed_pin)
            await error(409, service.prepare(principal, identity, release, {}, [], []))
            calls = 0

            async def final_changed_pin(session: AsyncSession, version_id: UUID) -> ChartPin:
                nonlocal calls
                calls += 1
                pin = await original_pin(session, version_id)
                return replace(pin, digest="sha256:" + "f" * 64) if calls == 2 else pin

            monkeypatch.setattr(charts, "lock_pin", final_changed_pin)
            await error(409, service.append(principal, identity, release, {}, [], []))
            monkeypatch.setattr(charts, "lock_pin", original_pin)

            def insertion_conflict(*args: object) -> None:
                raise IntegrityError("controlled", {}, RuntimeError("controlled"))

            event.listen(InstallationRevision, "after_insert", insertion_conflict)
            try:
                await error(409, service.append(principal, identity, release, {}, [], []))
            finally:
                event.remove(InstallationRevision, "after_insert", insertion_conflict)

            def insertion_cancel(*args: object) -> None:
                raise asyncio.CancelledError

            event.listen(InstallationRevision, "after_insert", insertion_cancel)
            try:
                with pytest.raises(asyncio.CancelledError):
                    await service.append(principal, identity, release, {}, [], [])
            finally:
                event.remove(InstallationRevision, "after_insert", insertion_cancel)

            started = asyncio.Event()

            async def downloading(pin: ChartPin) -> bytes:
                started.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

            monkeypatch.setattr(charts, "download_archive", downloading)
            task = asyncio.create_task(service.append(principal, identity, release, {}, [], []))
            await asyncio.wait_for(started.wait(), 5)
            assert isinstance(database.engine.pool, QueuePool)
            assert database.engine.pool.checkedout() == 0
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await service.history(principal, identity, limit=25, offset=0))[1] == 0

    asyncio.run(scenario())


def test_single_connection_and_batched_references(
    installation_api: InstallationAPI,
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential rechecks fit one connection and reference SQL count is independent of size."""
    from sqlalchemy import event
    from tinypods_core.applications.installation_contracts import SecretBinding
    from tinypods_core.applications.installation_service import InstallationService
    from tinypods_core.applications.installation_validation import InstallationValidator
    from tinypods_core.auth import SessionService
    from tinypods_core.auth.authorization import AuthorizationService
    from tinypods_core.db import Database
    from tinypods_core.repositories.verification import ChartVerification
    from tinypods_core.secrets.applications import ApplicationSecretService
    from tinypods_core.secrets.providers import encryption_provider

    api = installation_api
    settings = Settings.model_validate(
        {
            **key_settings(auth_settings).model_dump(),
            "database_pool_size": 1,
            "database_max_overflow": 0,
            "database_pool_timeout": 0.25,
        }
    )
    token = api.client.cookies["__Host-tinypods-session"]

    async def scenario() -> None:
        async with Database(settings) as database:
            sessions = SessionService(database)
            principal = await sessions.authenticate(token)
            authorization = AuthorizationService(sessions)
            charts = ChartVerification(
                database, authorization, api.client.app.state.repository_client
            )  # type: ignore[union-attr]
            provider = encryption_provider(settings)
            service = InstallationService(
                database, authorization, charts, InstallationValidator(), provider
            )
            identity = UUID(api.installation)
            # Before the fix, _installation held this pool's sole connection
            # while credential validation tried to check out a second one.
            await asyncio.wait_for(service.get(principal, identity), 2)
            await asyncio.wait_for(
                service.prepare(principal, identity, UUID(api.catalog.release), {}, [], []), 3
            )
            versions = []
            async with database.session() as session:
                secrets = ApplicationSecretService(session, authorization, provider)
                for index in range(12):
                    _, version = await secrets.save_application_version(
                        principal, identity, ("fixture-" + str(index)).encode()
                    )
                    versions.append(version.id)
            statements: list[str] = []

            def capture(*args: object) -> None:
                statements.append(str(args[2]))

            event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
            try:
                counts = []
                for size in (1, 12):
                    statements.clear()
                    async with database.session() as session:
                        installation = await service._installation(session, principal, identity)
                        values = await service._bindings(
                            session,
                            installation,
                            [
                                SecretBinding(version_id=version, target="field" + str(index))
                                for index, version in enumerate(versions[:size])
                            ],
                            [],
                            load_payloads=True,
                        )
                        assert len(values) == size
                    counts.append(len(statements))
                assert counts[0] == counts[1] == 3
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
            from sqlalchemy.pool import QueuePool
            from tinypods_core.secrets.crypto import Ciphertext, SecretContext

            original_decrypt = provider.decrypt

            async def detached_decrypt(ciphertext: Ciphertext, context: SecretContext) -> bytes:
                assert isinstance(database.engine.pool, QueuePool)
                assert database.engine.pool.checkedout() == 0
                return await original_decrypt(ciphertext, context)

            monkeypatch.setattr(provider, "decrypt", detached_decrypt)
            result = await service.prepare(
                principal,
                identity,
                UUID(api.catalog.release),
                {},
                [SecretBinding(version_id=versions[0], target="password")],
                [],
            )
            assert not result[1].issues

    asyncio.run(scenario())


def test_application_secret_fail_closed_inputs(
    installation_api: InstallationAPI,
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad payloads, absent scopes and revoked/corrupt versions cannot be resolved."""
    from tinypods_core.auth import SessionService
    from tinypods_core.auth.authorization import AuthorizationService
    from tinypods_core.db import Database
    from tinypods_core.secrets.applications import ApplicationSecretService
    from tinypods_core.secrets.crypto import Ciphertext, SecretContext, SecretError
    from tinypods_core.secrets.providers import EnvironmentProvider, encryption_provider

    api = installation_api
    settings = key_settings(auth_settings)
    token = api.client.cookies["__Host-tinypods-session"]

    async def scenario() -> None:
        async with Database(settings) as database:
            sessions = SessionService(database)
            principal = await sessions.authenticate(token)
            authorization = AuthorizationService(sessions)
            provider = encryption_provider(settings)
            async with database.session() as session:
                service = ApplicationSecretService(session, authorization, provider)
                identity = UUID(api.installation)
                assert service.authorization.in_session(session) is service.authorization
                for payload in (b"", b"x" * 262145, b"\xff"):
                    with pytest.raises(SecretError, match="invalid_secret_payload"):
                        await service.save_application_version(principal, identity, payload)
                with pytest.raises(SecretError, match="installation_not_found"):
                    await service.installation(principal, uuid4())
                with pytest.raises(SecretError, match="secret_not_found"):
                    await service.resolve_application_version(principal, identity, uuid4())
                scope, version = await service.save_application_version(
                    principal, identity, b"fixture"
                )
                await service.revoke_application_version(
                    principal, identity, scope.secret_id, version.id
                )
                with pytest.raises(SecretError, match="secret_revoked"):
                    await service.resolve_application_version(principal, identity, version.id)
                _, valid = await service.save_application_version(
                    principal, identity, b"fixture-two", secret_id=scope.secret_id
                )

                async def binary(
                    self: EnvironmentProvider, encrypted: Ciphertext, context: SecretContext
                ) -> bytes:
                    return b"\xff"

                monkeypatch.setattr(EnvironmentProvider, "decrypt", binary)
                with pytest.raises(SecretError, match="invalid_secret_payload"):
                    await service.resolve_application_version(principal, identity, valid.id)

                from tinypods_core.applications.installation_contracts import SecretBinding
                from tinypods_core.applications.installation_service import InstallationService
                from tinypods_core.applications.installation_validation import InstallationValidator
                from tinypods_core.repositories.verification import ChartVerification

                charts = ChartVerification(
                    database, authorization, api.client.app.state.repository_client
                )  # type: ignore[union-attr]
                installations = InstallationService(
                    database, authorization, charts, InstallationValidator(), provider
                )
                installation = await installations._installation(session, principal, identity)
                with pytest.raises(SecretError, match="invalid_secret_payload"):
                    await installations._confidential_values(
                        await installations._bindings(
                            session,
                            installation,
                            [SecretBinding(version_id=valid.id, target="password")],
                            [],
                            load_payloads=True,
                        )
                    )

    asyncio.run(scenario())


def test_binding_history_cannot_be_extended_later(
    installation_api: InstallationAPI,
    auth_settings: Settings,
) -> None:
    """A committed revision cannot acquire a new Kubernetes reference via direct SQL."""
    api = installation_api
    saved = api.client.post(api.path + "/revisions", json=api.revision()).json()
    with (
        psycopg.connect(auth_settings.database_url.get_secret_value()) as connection,
        pytest.raises(psycopg.IntegrityError),
    ):
        connection.execute(
            "INSERT INTO revision_kubernetes_secrets "
            "(id, created, last_updated, revision_id, target, use, name, key) "
            "VALUES (%s, now(), now(), %s, 'target', 'existingSecret', 'name', 'key')",
            (uuid4(), UUID(saved["id"])),
        )


def test_corrupted_secret_scope_rotation_fails_closed(auth_settings: Settings) -> None:
    """Even an inconsistent database cannot silently rotate a secret with missing scope."""
    from test_cluster_secrets import owner, service
    from tinypods_core.db import Database
    from tinypods_core.secrets.crypto import SecretError

    secret_id, version_id = uuid4(), uuid4()
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        # Fault injection is confined to the disposable schema. Runtime roles
        # cannot create this state through the deferred scope constraint.
        connection.execute("ALTER TABLE secrets DISABLE TRIGGER secrets_scope_check")
        connection.execute(
            "INSERT INTO secrets (id, created, last_updated, name, description, kind) "
            "VALUES (%s, now(), now(), 'Corrupt fixture', '', 'cluster')",
            (secret_id,),
        )
        connection.execute(
            "INSERT INTO secret_versions (id, created, last_updated, secret_id, version, "
            "encryption_key_id, nonce, encrypted_payload) "
            "VALUES (%s, now(), now(), %s, 1, 'old', %s, %s)",
            (version_id, secret_id, b"0" * 12, b"0" * 16),
        )
        connection.execute("ALTER TABLE secrets ENABLE TRIGGER secrets_scope_check")

    async def scenario() -> None:
        settings = key_settings(auth_settings, active="new", both=True)
        async with Database(settings) as database, database.session() as session:
            with pytest.raises(SecretError, match="secret_scope_missing"):
                await service(session, settings).rotate(owner())

    asyncio.run(scenario())


def test_recent_history_uses_index(
    installation_api: InstallationAPI, auth_settings: Settings
) -> None:
    """Newest-first history uses the installation/time/identity index backward."""
    api = installation_api
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            "INSERT INTO installation_revisions "
            "(id, created, last_updated, installation_id, release_id, overrides, values) "
            "SELECT gen_random_uuid(), now() + n * interval '1 microsecond', now(), "
            "%s, %s, '{}'::jsonb, '{}'::jsonb "
            "FROM generate_series(1, 1000) n",
            (UUID(api.installation), UUID(api.catalog.release)),
        )
        connection.execute("ANALYZE installation_revisions")
        plan = connection.execute(
            "EXPLAIN (FORMAT JSON) SELECT id FROM installation_revisions "
            "WHERE installation_id = %s ORDER BY created DESC, id DESC LIMIT 1",
            (UUID(api.installation),),
        ).fetchone()[0][0]["Plan"]
        pending = [plan]
        indexes = []
        while pending:
            node = pending.pop()
            if node.get("Index Name") == "ix_revision_history":
                indexes.append(node)
            pending.extend(node.get("Plans", []))
        assert len(indexes) == 1 and indexes[0]["Scan Direction"] == "Backward"


def test_secret_provider_failure_and_cancellation_are_atomic(
    installation_api: InstallationAPI,
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused service session cannot commit metadata left by failed/cancelled encryption."""
    from sqlalchemy import func
    from sqlmodel import select
    from tinypods_core.auth import SessionService
    from tinypods_core.auth.authorization import AuthorizationService
    from tinypods_core.db import Database
    from tinypods_core.secrets.applications import ApplicationSecretService
    from tinypods_core.secrets.crypto import Ciphertext, SecretContext, SecretError
    from tinypods_core.secrets.models import Secret, SecretVersion
    from tinypods_core.secrets.providers import encryption_provider

    api = installation_api
    settings = key_settings(auth_settings)
    token = api.client.cookies["__Host-tinypods-session"]

    async def scenario() -> None:
        async with Database(settings) as database, database.session() as session:
            sessions = SessionService(database)
            principal = await sessions.authenticate(token)
            provider = encryption_provider(settings)
            service = ApplicationSecretService(session, AuthorizationService(sessions), provider)
            original = provider.encrypt

            async def failed(payload: bytes, context: SecretContext) -> Ciphertext:
                raise SecretError("secret_key_unavailable", 503)

            monkeypatch.setattr(provider, "encrypt", failed)
            with pytest.raises(SecretError, match="secret_key_unavailable"):
                await service.save_application_version(
                    principal, UUID(api.installation), b"fixture"
                )
            started = asyncio.Event()

            async def blocked(payload: bytes, context: SecretContext) -> Ciphertext:
                started.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

            monkeypatch.setattr(provider, "encrypt", blocked)
            task = asyncio.create_task(
                service.save_application_version(principal, UUID(api.installation), b"fixture")
            )
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await session.exec(select(func.count()).select_from(Secret))).one() == 0
            assert (await session.exec(select(func.count()).select_from(SecretVersion))).one() == 0
            monkeypatch.setattr(provider, "encrypt", original)
            _, version = await service.save_application_version(
                principal, UUID(api.installation), b"fixture"
            )
            assert version.version == 1

    asyncio.run(scenario())
