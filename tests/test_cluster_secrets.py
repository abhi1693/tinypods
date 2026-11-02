"""Real PostgreSQL credential lifecycle, scope constraints, rotation and safe APIs."""

import asyncio
import base64
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from secret_fixtures import NEW_KEY, OLD_KEY, TOKEN, key_settings, kubeconfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from test_auth import auth_settings as auth_settings
from test_cluster_registry import client_for, create
from tinypods_core.audit import AuditRecord, audit_context
from tinypods_core.auth import Principal, SessionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.clusters import Cluster, Region
from tinypods_core.db import Database, MigrationManager
from tinypods_core.db.models import User
from tinypods_core.secrets.crypto import SecretError
from tinypods_core.secrets.kubernetes import ConnectionResult, Kubeconfig, KubernetesClient
from tinypods_core.secrets.models import ClusterSecret, Secret, SecretVersion
from tinypods_core.secrets.providers import encryption_provider
from tinypods_core.secrets.service import SecretService
from tinypods_core.settings import Settings

BASE = "/api/v1"
ROOT = Path(__file__).resolve().parents[1]


class Validator:
    """An internal trusted identity boundary, never an HTTP authentication fallback."""

    async def validate_principal(self, principal: Principal) -> Principal:
        """Use a command/test-owned identity for direct service regressions."""
        return principal


def owner() -> Principal:
    """Construct an internal test identity without changing any real user."""
    return Principal(UUID(int=0), "system:test-secrets", True, "test", credential_kind="test")


def service(session: AsyncSession, settings: Settings) -> SecretService:
    """Compose the same storage service as HTTP with deterministic fixture keys."""
    return SecretService(session, AuthorizationService(Validator()), encryption_provider(settings))


async def cluster_records(database: Database) -> tuple[UUID, UUID]:
    """Create two real scoped cluster IDs in an otherwise disposable schema."""
    async with database.transaction() as session:
        region = Region(name="Local", slug="local")
        session.add(region)
        await session.flush()
        first = Cluster(name="Cluster 1", slug="cluster-1", region_id=region.id)
        second = Cluster(name="Cluster 2", slug="cluster-2", region_id=region.id)
        session.add_all([first, second])
    return first.id, second.id


def test_owner_http_lifecycle_and_no_plaintext(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Manage only clusters and versions; internal secret records never enter public DTOs."""
    settings = key_settings(auth_settings)

    async def connected(self: KubernetesClient, config: Kubeconfig) -> ConnectionResult:
        """Control outbound I/O here; separate tests exercise real official-SDK TLS calls."""
        assert config.token is not None
        return ConnectionResult(True, "connected", "v1.33.1")

    monkeypatch.setattr(KubernetesClient, "test", connected)
    with client_for(settings) as client:
        region = create(client, "regions", name="Local", slug="local")
        first = create(
            client, "clusters", name="Cluster 1", slug="cluster-1", region_id=region["id"]
        )
        second = create(
            client, "clusters", name="Cluster 2", slug="cluster-2", region_id=region["id"]
        )
        path = BASE + "/clusters/" + first["id"] + "/kubeconfig"
        other = BASE + "/clusters/" + second["id"] + "/kubeconfig"
        history = path + "/versions"
        assert client.get(path).status_code == 404
        uploaded = client.post(
            history,
            json={"encoding": "base64", "kubeconfig": base64.b64encode(kubeconfig()).decode()},
        )
        assert uploaded.status_code == 201, uploaded.text
        one = uploaded.json()
        assert one["version"] == 1 and "secret" not in one and "secret_id" not in one
        metadata = client.get(path).json()
        assert metadata["cluster"]["region"]["name"] == "Local"
        assert metadata["active_version"]["id"] == one["id"]
        assert "id" not in metadata and "secret" not in metadata
        assert (
            TOKEN not in uploaded.text
            and "nonce" not in uploaded.text
            and "encrypted_payload" not in uploaded.text
        )
        assert client.get(BASE + "/cluster-secrets").status_code == 404
        assert client.get(BASE + "/secret-versions").status_code == 404
        two = client.post(
            history,
            json={
                "encoding": "base64",
                "kubeconfig": base64.b64encode(kubeconfig(token=TOKEN + "-two")).decode(),
            },
        ).json()
        three = client.post(
            history,
            json={
                "encoding": "base64",
                "kubeconfig": base64.b64encode(kubeconfig(token=TOKEN + "-three")).decode(),
                "activate": False,
            },
        ).json()
        assert (two["version"], three["version"]) == (2, 3)
        assert client.get(path).json()["active_version"]["id"] == two["id"]
        other_version = client.post(
            other + "/versions",
            json={
                "encoding": "base64",
                "kubeconfig": base64.b64encode(kubeconfig()).decode(),
                "activate": False,
            },
        )
        assert other_version.status_code == 201
        assert client.get(other).json()["active_version"] is None
        for suffix in ("", "/activate", "/revoke"):
            route = other + "/versions/" + one["id"] + suffix
            response = client.get(route) if not suffix else client.post(route)
            assert response.status_code == 404
        assert client.get(history + "/" + one["id"]).json()["id"] == one["id"]
        assert (
            client.post(history + "/" + three["id"] + "/activate").json()["active_version"]["id"]
            == three["id"]
        )
        revoked = client.post(history + "/" + three["id"] + "/revoke")
        assert revoked.status_code == 200 and revoked.json()["revoked_at"]
        assert (
            client.post(history + "/" + three["id"] + "/revoke").json()["revoked_at"]
            == revoked.json()["revoked_at"]
        )
        assert (
            client.post(BASE + "/clusters/" + first["id"] + "/testConnection").json()["code"]
            == "secret_revoked"
        )
        assert client.post(history + "/" + three["id"] + "/activate").status_code == 409
        assert client.post(history + "/" + one["id"] + "/activate").status_code == 200
        assert client.post(BASE + "/clusters/" + first["id"] + "/testConnection").json()[
            "connected"
        ]
        assert (
            client.post(BASE + "/clusters/" + second["id"] + "/testConnection").status_code == 404
        )
        assert client.delete(BASE + "/clusters/" + first["id"]).status_code == 409
        assert client.delete(path).status_code == 405
        page = client.get(history, params={"limit": 1, "offset": 1}).json()
        assert page["total"] == 3 and page["items"][0]["version"] == 2
        for suffix in ("", "/activate", "/revoke"):
            missing = history + "/" + str(uuid4()) + suffix
            assert (client.get(missing) if not suffix else client.post(missing)).status_code == 404
        absent = BASE + "/clusters/" + str(uuid4()) + "/kubeconfig"
        assert client.get(absent).status_code == 404
        assert (
            client.post(
                absent + "/versions",
                json={"encoding": "base64", "kubeconfig": base64.b64encode(kubeconfig()).decode()},
            ).status_code
            == 404
        )
        assert client.post(history, json={"kubeconfig": "invalid " + TOKEN}).status_code == 422
        assert (
            client.post(
                history,
                json={
                    "encoding": "base64",
                    "kubeconfig": base64.b64encode(kubeconfig()).decode(),
                    "secret_id": str(uuid4()),
                },
            ).status_code
            == 422
        )
        schema = client.get("/openapi.json").json()
        assert schema["components"]["schemas"]["KubeconfigWrite"]["properties"]["kubeconfig"][
            "writeOnly"
        ]
        assert "encrypted_payload" not in json.dumps(
            schema["components"]["schemas"]["KubeconfigVersionRead"]
        )

    async def inspect_storage() -> None:
        database = Database(settings)
        try:
            async with database.session() as session:
                values = (
                    await session.exec(text("SELECT nonce, encrypted_payload FROM secret_versions"))
                ).all()
                assert len(values) == 4 and all(len(row.nonce) == 12 for row in values)
                assert all(TOKEN.encode() not in row.encrypted_payload for row in values)
                audits = (await session.exec(select(AuditRecord))).all()
                serialized = json.dumps([row.model_dump(mode="json") for row in audits])
                assert TOKEN not in serialized and OLD_KEY not in serialized
                assert (
                    "encrypted_payload" not in serialized
                    and "nonce" not in serialized
                    and "kubeconfig" not in serialized.lower().replace("kubernetes kubeconfig", "")
                )
                version = (await session.exec(select(SecretVersion))).first()
                assert (
                    version is not None
                    and {"nonce", "encrypted_payload"} <= inspect(version).unloaded
                )
        finally:
            await database.close()

    asyncio.run(inspect_storage())
    assert TOKEN not in caplog.text and OLD_KEY not in caplog.text


def test_reader_and_missing_keys_fail_closed(auth_settings: Settings) -> None:
    """Require owners, keep invalid confidential input private and fail closed without keys."""
    with client_for(auth_settings) as client:
        region = create(client, "regions", name="Local", slug="local")
        cluster = create(
            client, "clusters", name="Cluster 1", slug="cluster-1", region_id=region["id"]
        )
        path = BASE + "/clusters/" + cluster["id"] + "/kubeconfig"
        assert (
            client.post(
                path + "/versions",
                json={"encoding": "base64", "kubeconfig": base64.b64encode(kubeconfig()).decode()},
            ).status_code
            == 503
        )
        malformed = json.dumps({"kubeconfig": TOKEN + chr(0xD800)}, ensure_ascii=True).encode()
        invalid = client.post(
            path + "/versions", content=malformed, headers={"content-type": "application/json"}
        )
        assert invalid.status_code == 422 and TOKEN not in invalid.text

        async def reader_session() -> tuple[str, str]:
            database = Database(auth_settings)
            try:
                async with database.transaction() as session:
                    reader = User(username="reader", is_owner=False)
                    session.add(reader)
                issued = await SessionService(database, settings=auth_settings).issue(reader.id)
                return issued.token, issued.csrf_token
            finally:
                await database.close()

        token, csrf = asyncio.run(reader_session())
        client.cookies.clear()
        client.cookies.set("__Host-tinypods-session", token)
        client.headers["x-csrf-token"] = csrf
        for route in (path, path + "/versions", path + "/versions/" + str(uuid4())):
            assert client.get(route).status_code == 403
        assert (
            client.post(
                path + "/versions",
                json={"encoding": "base64", "kubeconfig": base64.b64encode(kubeconfig()).decode()},
            ).status_code
            == 403
        )
        assert (
            client.post(BASE + "/clusters/" + cluster["id"] + "/testConnection").status_code == 403
        )
        client.cookies.clear()
        assert client.get(path).status_code == 401


def test_version_races_scope_guards_and_rotation(auth_settings: Settings) -> None:
    """Serialize version allocation and rotate encrypted bytes without changing logical pins."""
    settings = key_settings(auth_settings)

    async def scenario() -> None:
        database = Database(settings)
        try:
            first, second = await cluster_records(database)
            async with database.session() as session:
                binding = await service(session, settings).create(
                    owner(), first, "First", "", kubeconfig()
                )

            async def append(token: str) -> SecretVersion:
                """Use independent sessions as concurrent HTTP requests would."""
                async with database.session() as session:
                    return await service(session, settings).replace(
                        owner(), binding.secret_id, kubeconfig(token=token)
                    )

            versions = await asyncio.gather(append(TOKEN + "-one"), append(TOKEN + "-two"))
            assert sorted(version.version for version in versions) == [2, 3]
            async with database.session() as session:
                rows, total = await service(session, settings).versions(
                    owner(), limit=100, offset=0
                )
                assert len(rows) == total == 3
                active = (await session.get(ClusterSecret, binding.id)).active_version_id
                identities = {row.id for row in rows}
            new_settings = key_settings(settings, active="new", both=True)
            async with database.session() as session:
                progress = await service(session, new_settings).rotate(owner(), 1)
                assert progress.updated == 1 and progress.remaining == 2

            async def rotate_one() -> int:
                """Independent operators skip locked rows without hiding incomplete work."""
                async with database.session() as session:
                    return (await service(session, new_settings).rotate(owner(), 1)).updated

            assert sum(await asyncio.gather(rotate_one(), rotate_one())) == 2
            async with database.session() as session:
                current = service(session, new_settings)
                result = await current.rotate(owner(), 100)
                assert asdict(result) == {"updated": 0, "remaining": 0, "active_key_id": "new"}
                rows, total = await current.versions(owner(), limit=100, offset=0)
                assert {row.id for row in rows} == identities
                assert all(row.encryption_key_id == "new" for row in rows)
                assert (await session.get(ClusterSecret, binding.id)).active_version_id == active
                for row in rows:
                    encrypted = await current._payload_version(row.id)
                    original = await current._resolve(encrypted, binding)
                    assert json.loads(original)["users"][0]["user"]["token"] in {
                        TOKEN,
                        TOKEN + "-one",
                        TOKEN + "-two",
                    }
                with pytest.raises(ValueError):
                    await current.versions(owner(), limit=0, offset=0)
                with pytest.raises(ValueError):
                    await current.rotate(owner(), 0)
            async with database.session() as session:
                with pytest.raises(SecretError, match="key_unavailable"):
                    await service(session, Settings()).rotate(owner())
            async with database.session() as session:
                other = await service(session, settings).create(
                    owner(), second, "Second", "", kubeconfig()
                )
            statements = [
                (
                    "UPDATE cluster_secrets SET active_version_id=:version WHERE id=:id",
                    {"version": other.active_version_id, "id": binding.id},
                ),
                (
                    "UPDATE cluster_secrets SET cluster_id=:cluster WHERE id=:id",
                    {"cluster": second, "id": binding.id},
                ),
                ("UPDATE secret_versions SET version=version+100 WHERE id=:id", {"id": active}),
                ("DELETE FROM cluster_secrets WHERE id=:id", {"id": binding.id}),
            ]
            for statement, parameters in statements:
                with pytest.raises(IntegrityError):
                    async with database.transaction() as session:
                        await session.exec(text(statement), params=parameters)
            with pytest.raises(IntegrityError):
                async with database.transaction() as session:
                    session.add(Secret(name="Dangling"))
            async with database.session() as session:
                revoked = await service(session, new_settings).revoke(owner(), active)
                assert revoked.revoked_at is not None
            with pytest.raises(IntegrityError):
                async with database.transaction() as session:
                    await session.exec(
                        text("UPDATE secret_versions SET revoked_at=NULL WHERE id=:id"),
                        params={"id": active},
                    )
            MigrationManager(settings, ROOT / "alembic.ini").check()
        finally:
            await database.close()

    with audit_context("system:test-secrets"):
        asyncio.run(scenario())


def test_direct_handlers_nullable_active_scope_and_commit_recovery(auth_settings: Settings) -> None:
    """Await actual cluster-focused handlers and verify internal transaction recovery."""
    from tinypods_api.http import PageParams
    from tinypods_api.secrets import routes
    from tinypods_api.secrets.contracts import KubeconfigWrite

    class Client(KubernetesClient):
        async def test(self, config: Kubeconfig) -> ConnectionResult:
            """Control only external I/O; this path still resolves real encrypted storage."""
            return ConnectionResult(True, "connected", "v1.33.1")

    async def scenario() -> None:
        settings = key_settings(auth_settings)
        database = Database(settings)
        try:
            first, second = await cluster_records(database)
            async with database.session() as session:
                with pytest.raises(SecretError, match="kubeconfig_not_found"):
                    await service(session, settings).cluster_binding(owner(), first)
            async with database.session() as session:
                version = await routes.upload_kubeconfig(
                    first,
                    KubeconfigWrite(
                        encoding="base64",
                        kubeconfig=SecretStr(base64.b64encode(kubeconfig()).decode()),
                    ),
                    service(session, settings),
                    owner(),
                )
            async with database.session() as session:
                current = service(session, settings)
                binding = await current.cluster_binding(owner(), first)
                metadata = await routes.get_kubeconfig(first, current, owner())
                assert metadata.cluster.id == first and metadata.active_version.id == version.id
                assert (
                    await routes.list_versions(first, PageParams(), current, owner())
                ).total == 1
                assert (await routes.get_version(first, version.id, current, owner())).version == 1
                assert (
                    await routes.activate_version(first, version.id, current, owner())
                ).active_version.id == version.id
            async with database.session() as session:
                current = SecretService(
                    session,
                    AuthorizationService(Validator()),
                    encryption_provider(settings),
                    Client(),
                )
                assert (await routes.test_connection(first, current, owner())).connected
            async with database.session() as session:
                current = service(session, settings)
                revoked = await routes.revoke_version(first, version.id, current, owner())
                assert revoked.revoked_at
                assert (await current.revoke(owner(), version.id)).revoked_at == revoked.revoked_at
            async with database.session() as session:
                current = service(session, settings)
                await routes.upload_kubeconfig(
                    second,
                    KubeconfigWrite(
                        encoding="base64",
                        kubeconfig=SecretStr(base64.b64encode(kubeconfig()).decode()),
                        activate=False,
                    ),
                    current,
                    owner(),
                )
                assert (
                    await routes.get_kubeconfig(second, current, owner())
                ).active_version is None
                with pytest.raises(SecretError):
                    await current.test(owner(), second)
                for operation in (
                    current.get(owner(), ClusterSecret, uuid4()),
                    current.create(owner(), uuid4(), "Missing", "", kubeconfig()),
                    current.replace(owner(), uuid4(), kubeconfig()),
                    current.activate(owner(), uuid4(), version.id),
                    current.revoke(owner(), uuid4()),
                    current._payload_version(uuid4()),
                    current.cluster_binding(owner(), uuid4()),
                    current.save_cluster_version(owner(), uuid4(), kubeconfig()),
                    current.cluster_version(owner(), second, version.id),
                ):
                    with pytest.raises(SecretError):
                        await operation
            async with database.session() as session:
                current = service(session, settings)
                with pytest.raises(SecretError, match="secret_conflict"):
                    await current.create(owner(), first, "Duplicate", "", kubeconfig())
                alien = SecretVersion(
                    secret_id=uuid4(),
                    version=1,
                    encryption_key_id="old",
                    nonce=b"",
                    encrypted_payload=b"",
                )
                with pytest.raises(SecretError, match="scope_mismatch"):
                    await current._resolve(alien, binding)
                replacement = await current.save_cluster_version(
                    owner(), second, kubeconfig(), activate=False
                )
                assert replacement.version == 2
                duplicate = await current._version(binding.secret_id, first, 1, kubeconfig())
                session.add(duplicate)
                with pytest.raises(SecretError, match="conflict"):
                    await current._commit()
                assert not session.in_transaction()
                assert await session.get(ClusterSecret, binding.id) is not None
                assert (await current.bindings(owner(), limit=100, offset=0))[1] == 2
                assert (await current.bindings(owner(), limit=100, offset=0, cluster_id=first))[
                    1
                ] == 1
            async with database.session() as session:
                provisional = Secret(name="Not yet scoped")
                session.add(provisional)
                await session.flush()
                with pytest.raises(SecretError, match="scope_missing"):
                    await service(session, settings)._locked_scope(provisional.id)
        finally:
            await database.close()

    with audit_context("system:test-secrets"):
        asyncio.run(scenario())


def test_cli_key_rotation_and_missing_historical_key(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The real operator command reports resumable progress and fails without leaking keys."""
    from tinypods_cli.main import app
    from typer.testing import CliRunner

    settings = key_settings(auth_settings)

    async def seed() -> None:
        database = Database(settings)
        try:
            first, _ = await cluster_records(database)
            async with database.session() as session:
                await service(session, settings).create(owner(), first, "CLI", "", kubeconfig())
        finally:
            await database.close()

    with audit_context("system:test-cli-secrets"):
        asyncio.run(seed())
    monkeypatch.setenv("TINYPODS_DATABASE_URL", auth_settings.database_url.get_secret_value())
    monkeypatch.setenv("TINYPODS_VALKEY_URL", auth_settings.valkey_url.get_secret_value())
    monkeypatch.setenv("TINYPODS_SECRET_KEYS", json.dumps({"new": NEW_KEY}))
    monkeypatch.setenv("TINYPODS_SECRET_ACTIVE_KEY_ID", "new")
    runner = CliRunner()
    failed = runner.invoke(app, ["secrets", "rotate-key"])
    assert failed.exit_code == 1 and "secret_key_unavailable" in failed.stdout
    assert TOKEN not in failed.output and NEW_KEY not in failed.output
    monkeypatch.setenv("TINYPODS_SECRET_KEYS", json.dumps({"old": OLD_KEY, "new": NEW_KEY}))
    result = runner.invoke(app, ["secrets", "rotate-key", "--batch-size", "1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "status": "rotated",
        "updated": 1,
        "remaining": 0,
        "active_key_id": "new",
    }
    assert (
        TOKEN not in result.output and OLD_KEY not in result.output and NEW_KEY not in result.output
    )
    assert TOKEN not in caplog.text


def test_populated_credential_migration_downgrade_and_upgrade(auth_settings: Settings) -> None:
    """One revision upgrades populated registry data and cleanly removes only its own tables."""
    from alembic import command

    settings = key_settings(auth_settings)
    manager = MigrationManager(settings, ROOT / "alembic.ini")

    async def seed() -> tuple[UUID, UUID]:
        database = Database(settings)
        try:
            return await cluster_records(database)
        finally:
            await database.close()

    with audit_context("system:test-secret-migrations"):
        identities = asyncio.run(seed())
    command.downgrade(manager.config(), "0006_regions_clusters")
    manager.upgrade()
    manager.check()

    async def verify() -> None:
        database = Database(settings)
        try:
            async with database.session() as session:
                assert all(
                    [await session.get(Cluster, identity) is not None for identity in identities]
                )
                triggers = (
                    await session.exec(
                        text(
                            "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
                            "('secrets'::regclass, 'secret_versions'::regclass, "
                            "'cluster_secrets'::regclass) AND NOT tgisinternal"
                        )
                    )
                ).all()
                assert {row[0] for row in triggers} == {
                    "secrets_scope_check",
                    "cluster_secrets_scope_check",
                    "secret_versions_immutable",
                    "cluster_secrets_immutable",
                }
        finally:
            await database.close()

    asyncio.run(verify())


def test_rotation_skips_locks_and_accepts_new_key_writes(auth_settings: Settings) -> None:
    """A partial batch reports held rows while concurrent replacements use the new key."""
    settings = key_settings(auth_settings)
    new_settings = key_settings(settings, active="new", both=True)

    async def scenario() -> None:
        database = Database(settings)
        try:
            first, _ = await cluster_records(database)
            async with database.session() as session:
                binding = await service(session, settings).create(
                    owner(), first, "Pi", "", kubeconfig()
                )
            async with database.session() as session:
                await service(session, settings).replace(owner(), binding.secret_id, kubeconfig())
            async with database.session() as held:
                locked = (
                    await held.exec(
                        select(SecretVersion).order_by(SecretVersion.id).limit(1).with_for_update()
                    )
                ).one()
                async with database.session() as session:
                    partial = await service(session, new_settings).rotate(owner(), 100)
                    assert partial.updated == 1 and partial.remaining == 1
                async with database.session() as session:
                    added = await service(session, new_settings).replace(
                        owner(), binding.secret_id, kubeconfig()
                    )
                    assert added.version == 3 and added.encryption_key_id == "new"
                assert locked.encryption_key_id == "old"
            async with database.session() as session:
                done = await service(session, new_settings).rotate(owner(), 100)
                assert done.updated == 1 and done.remaining == 0
        finally:
            await database.close()

    with audit_context("system:test-rotation-locks"):
        asyncio.run(scenario())


def test_tampered_batch_rolls_back_and_never_activates(auth_settings: Settings) -> None:
    """Corruption cannot resolve/activate, and a late failure rolls back earlier batch edits."""
    settings = key_settings(auth_settings)
    new_settings = key_settings(settings, active="new", both=True)

    async def scenario() -> None:
        database = Database(settings)
        try:
            first, _ = await cluster_records(database)
            async with database.session() as session:
                binding = await service(session, settings).create(
                    owner(), first, "Pi", "", kubeconfig()
                )
            async with database.session() as session:
                await service(session, settings).replace(
                    owner(), binding.secret_id, kubeconfig(), activate=False
                )
            async with database.transaction() as session:
                identities = (
                    await session.exec(select(SecretVersion.id).order_by(SecretVersion.id))
                ).all()
                corrupted = identities[-1]
                await session.exec(
                    text("UPDATE secret_versions SET encrypted_payload=:payload WHERE id=:id"),
                    params={"payload": b"x" * 64, "id": corrupted},
                )
            async with database.session() as session:
                current = service(session, settings)
                with pytest.raises(SecretError, match="integrity_failed"):
                    await current.activate(owner(), binding.id, corrupted)
            async with database.session() as session:
                current = service(session, new_settings)
                with pytest.raises(SecretError, match="integrity_failed"):
                    await current.rotate(owner(), 100)
                assert not session.in_transaction()
                rows, _ = await current.versions(owner(), limit=100, offset=0)
                assert all(row.encryption_key_id == "old" for row in rows)
        finally:
            await database.close()

    with audit_context("system:test-tampering"):
        asyncio.run(scenario())
