"""Real PostgreSQL credential lifecycle, scope constraints, rotation and safe APIs."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest
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
from tinypods_core.secrets.crypto import SecretError, SecretKeyring
from tinypods_core.secrets.kubernetes import ConnectionResult, Kubeconfig, KubernetesClient
from tinypods_core.secrets.models import ClusterSecret, Secret, SecretVersion
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
    return SecretService(session, AuthorizationService(Validator()), SecretKeyring(settings))


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
    """Exercise all public metadata/actions and prove payloads stay outside responses/audit."""
    settings = key_settings(auth_settings)

    async def connected(self: KubernetesClient, config: Kubeconfig) -> ConnectionResult:
        """Control external I/O here; separate tests verify actual TLS exchanges."""
        assert config.token is not None
        assert config.token.get_secret_value() in {TOKEN, TOKEN + "-replacement", TOKEN + "-staged"}
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
        response = client.post(
            BASE + "/cluster-secrets",
            json={
                "cluster_id": first["id"],
                "name": "Pi credentials",
                "kubeconfig": kubeconfig().decode(),
            },
        )
        assert response.status_code == 201, response.text
        binding = response.json()
        assert binding["cluster"]["region"]["name"] == "Local"
        assert binding["secret"]["kind"] == "cluster"
        assert binding["active_version"]["version"] == 1
        assert (
            TOKEN not in response.text
            and "encrypted_payload" not in response.text
            and "nonce" not in response.text
        )
        identity, secret_id = binding["id"], binding["secret"]["id"]
        first_version = binding["active_version"]["id"]
        assert client.get(BASE + "/cluster-secrets/" + identity).json() == binding
        assert (
            client.get(BASE + "/cluster-secrets", params={"cluster_id": first["id"]}).json()[
                "total"
            ]
            == 1
        )
        assert (
            client.get(BASE + "/secret-versions", params={"secret_id": secret_id}).json()["total"]
            == 1
        )
        assert client.get(BASE + "/secret-versions/" + first_version).status_code == 200
        assert client.post(BASE + "/clusters/" + first["id"] + "/testConnection").json() == {
            "connected": True,
            "reason": "connected",
            "kubernetes_version": "v1.33.1",
        }
        assert (
            client.post(BASE + "/clusters/" + second["id"] + "/testConnection").status_code == 404
        )
        assert (
            client.post(
                BASE + "/cluster-secrets",
                json={
                    "cluster_id": first["id"],
                    "name": "Duplicate",
                    "kubeconfig": kubeconfig().decode(),
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                BASE + "/cluster-secrets",
                json={
                    "cluster_id": str(uuid4()),
                    "name": "Missing",
                    "kubeconfig": kubeconfig().decode(),
                },
            ).status_code
            == 404
        )
        assert (
            client.post(
                BASE + "/cluster-secrets",
                json={
                    "cluster_id": second["id"],
                    "name": "Invalid",
                    "kubeconfig": "bad input " + TOKEN,
                },
            ).status_code
            == 422
        )
        assert client.get(BASE + "/cluster-secrets/" + str(uuid4())).status_code == 404
        assert client.get(BASE + "/secret-versions/" + str(uuid4())).status_code == 404
        replaced = client.post(
            BASE + "/secret-versions",
            json={
                "secret_id": secret_id,
                "kubeconfig": kubeconfig(token=TOKEN + "-replacement").decode(),
            },
        )
        assert replaced.status_code == 201, replaced.text
        replacement = replaced.json()
        staged = client.post(
            BASE + "/secret-versions",
            json={
                "secret_id": secret_id,
                "kubeconfig": kubeconfig(token=TOKEN + "-staged").decode(),
                "activate": False,
            },
        )
        assert staged.status_code == 201, staged.text
        assert staged.json()["version"] == 3
        assert (
            client.get(BASE + "/cluster-secrets/" + identity).json()["active_version"]["id"]
            == replacement["id"]
        )
        other = client.post(
            BASE + "/cluster-secrets",
            json={
                "cluster_id": second["id"],
                "name": "Other credentials",
                "kubeconfig": kubeconfig().decode(),
            },
        ).json()
        assert (
            client.post(
                BASE + "/cluster-secrets/" + identity + "/activate",
                json={"version_id": other["active_version"]["id"]},
            ).status_code
            == 409
        )
        assert (
            client.post(
                BASE + "/cluster-secrets/" + str(uuid4()) + "/activate",
                json={"version_id": first_version},
            ).status_code
            == 404
        )
        assert (
            client.post(
                BASE + "/secret-versions",
                json={"secret_id": str(uuid4()), "kubeconfig": kubeconfig().decode()},
            ).status_code
            == 404
        )
        assert client.post(BASE + "/secret-versions/" + str(uuid4()) + "/revoke").status_code == 404
        revoked = client.post(BASE + "/secret-versions/" + replacement["id"] + "/revoke")
        assert revoked.status_code == 200 and revoked.json()["revoked_at"]
        again = client.post(BASE + "/secret-versions/" + replacement["id"] + "/revoke")
        assert again.json()["revoked_at"] == revoked.json()["revoked_at"]
        assert (
            client.post(BASE + "/clusters/" + first["id"] + "/testConnection").json()["code"]
            == "secret_revoked"
        )
        assert (
            client.post(
                BASE + "/cluster-secrets/" + identity + "/activate",
                json={"version_id": first_version},
            ).status_code
            == 200
        )
        assert (
            client.post(
                BASE + "/cluster-secrets/" + identity + "/activate",
                json={"version_id": replacement["id"]},
            ).status_code
            == 409
        )
        assert (
            client.post(
                BASE + "/cluster-secrets/" + identity + "/activate",
                json={"version_id": str(uuid4())},
            ).status_code
            == 404
        )
        assert client.delete(BASE + "/clusters/" + first["id"]).status_code == 409
        assert client.delete(BASE + "/cluster-secrets/" + identity).status_code == 405
        assert (
            client.get(BASE + "/secret-versions", params={"secret_id": secret_id}).json()["total"]
            == 3
        )
        schema = client.get("/openapi.json").json()
        assert schema["components"]["schemas"]["ClusterSecretWrite"]["properties"]["kubeconfig"][
            "writeOnly"
        ]
        assert "encrypted_payload" not in json.dumps(
            schema["components"]["schemas"]["SecretVersionRead"]
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
                    and "kubeconfig" not in serialized
                )
                version = (await session.exec(select(SecretVersion))).first()
                assert version is not None
                assert {"nonce", "encrypted_payload"} <= inspect(version).unloaded
        finally:
            await database.close()

    asyncio.run(inspect_storage())
    assert TOKEN not in caplog.text and OLD_KEY not in caplog.text


def test_reader_and_missing_keys_fail_closed(auth_settings: Settings) -> None:
    """Metadata is owner-only and unconfigured credential writes cannot become plaintext."""
    with client_for(auth_settings) as client:
        region = create(client, "regions", name="Local", slug="local")
        cluster = create(
            client, "clusters", name="Cluster 1", slug="cluster-1", region_id=region["id"]
        )
        assert (
            client.post(
                BASE + "/cluster-secrets",
                json={
                    "cluster_id": cluster["id"],
                    "name": "No keys",
                    "kubeconfig": kubeconfig().decode(),
                },
            ).status_code
            == 503
        )
        assert client.get(BASE + "/cluster-secrets").json()["total"] == 0
        malformed = json.dumps(
            {"cluster_id": cluster["id"], "name": "Invalid", "kubeconfig": TOKEN + chr(0xD800)},
            ensure_ascii=True,
        ).encode()
        invalid = client.post(
            BASE + "/cluster-secrets",
            content=malformed,
            headers={"content-type": "application/json"},
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
        for path in ("/cluster-secrets", "/secret-versions", "/secret-versions/" + str(uuid4())):
            assert client.get(BASE + path).status_code == 403
        assert (
            client.post(BASE + "/clusters/" + cluster["id"] + "/testConnection").status_code == 403
        )
        client.cookies.clear()
        assert client.get(BASE + "/cluster-secrets").status_code == 401


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
                    original = current._resolve(encrypted, binding)
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
    """Await the actual typed handlers and recover after a real uniqueness failure."""
    from pydantic import SecretStr
    from tinypods_api.secrets import routes
    from tinypods_api.secrets.contracts import (
        ActivateWrite,
        ClusterSecretQuery,
        ClusterSecretWrite,
        SecretVersionQuery,
        SecretVersionWrite,
    )
    from tinypods_core.secrets.kubernetes import ConnectionResult

    class Client(KubernetesClient):
        """Control only the external boundary while exercising real decryption and APIs."""

        async def test(self, config: Kubeconfig) -> ConnectionResult:
            """Observe a correctly resolved credential without exposing its contents."""
            assert config.token is not None
            return ConnectionResult(True, "connected", "v1.33.1")

    async def scenario() -> None:
        settings = key_settings(auth_settings)
        database = Database(settings)
        try:
            first, second = await cluster_records(database)
            async with database.session() as session:
                current = service(session, settings)
                binding = await routes.create_binding(
                    ClusterSecretWrite(
                        cluster_id=first, name="Pi", kubeconfig=SecretStr(kubeconfig().decode())
                    ),
                    current,
                    owner(),
                )
            async with database.session() as session:
                current = service(session, settings)
                assert (await routes.get_binding(binding.id, current, owner())).id == binding.id
                page = await routes.list_bindings(ClusterSecretQuery(), current, owner())
                assert page.total == 1 and page.items[0].cluster.id == first
            async with database.session() as session:
                current = service(session, settings)
                version = await routes.create_version(
                    SecretVersionWrite(
                        secret_id=binding.secret.id,
                        kubeconfig=SecretStr(kubeconfig().decode()),
                        activate=False,
                    ),
                    current,
                    owner(),
                )
            async with database.session() as session:
                current = service(session, settings)
                assert (await routes.get_version(version.id, current, owner())).version == 2
                page = await routes.list_versions(SecretVersionQuery(), current, owner())
                assert page.total == 2
            async with database.session() as session:
                active = await routes.activate_binding(
                    binding.id,
                    ActivateWrite(version_id=version.id),
                    service(session, settings),
                    owner(),
                )
                assert active.active_version.id == version.id
            async with database.session() as session:
                current = SecretService(
                    session, AuthorizationService(Validator()), SecretKeyring(settings), Client()
                )
                checked = await routes.test_connection(first, current, owner())
                assert checked.connected and checked.kubernetes_version == "v1.33.1"
            async with database.session() as session:
                revoked = await routes.revoke_version(
                    version.id, service(session, settings), owner()
                )
                assert revoked.revoked_at
                again = await service(session, settings).revoke(owner(), version.id)
                assert again.revoked_at == revoked.revoked_at
            async with database.session() as session:
                current = service(session, settings)
                with pytest.raises(SecretError, match="not_found"):
                    await current.get(owner(), ClusterSecret, uuid4())
                with pytest.raises(SecretError, match="cluster_not_found"):
                    await current.create(owner(), uuid4(), "Missing", "", kubeconfig())
                with pytest.raises(SecretError, match="not_found"):
                    await current.replace(owner(), uuid4(), kubeconfig())
                with pytest.raises(SecretError, match="not_found"):
                    await current.activate(owner(), uuid4(), version.id)
                with pytest.raises(SecretError, match="not_found"):
                    await current.revoke(owner(), uuid4())
                with pytest.raises(SecretError, match="not_found"):
                    await current._payload_version(uuid4())
            async with database.transaction() as session:
                row = await session.get(ClusterSecret, binding.id)
                row.active_version_id = None
                session.add(row)
            async with database.session() as session:
                current = service(session, settings)
                assert (
                    await routes.get_binding(binding.id, current, owner())
                ).active_version is None
                with pytest.raises(SecretError, match="not_found"):
                    await current.test(owner(), first)
            async with database.session() as session:
                current = service(session, settings)
                duplicate = current._version(binding.secret.id, first, 1, kubeconfig())
                session.add(duplicate)
                with pytest.raises(SecretError, match="conflict"):
                    await current._commit()
                assert not session.in_transaction()
                assert await session.get(ClusterSecret, binding.id) is not None
            async with database.session() as session:
                provisional = Secret(name="Not yet scoped")
                session.add(provisional)
                await session.flush()
                with pytest.raises(SecretError, match="scope_missing"):
                    await service(session, settings)._locked_scope(provisional.id)
                # The provisional transaction is rolled back by this session scope.

            async def competing_create() -> ClusterSecret | SecretError:
                """Compete on one real cluster row using independent request sessions."""
                async with database.session() as session:
                    try:
                        return await service(session, settings).create(
                            owner(), second, "Race", "", kubeconfig()
                        )
                    except SecretError as exc:
                        return exc

            outcomes = await asyncio.gather(competing_create(), competing_create())
            assert sum(isinstance(value, ClusterSecret) for value in outcomes) == 1
            assert [value.status for value in outcomes if isinstance(value, SecretError)] == [409]
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
