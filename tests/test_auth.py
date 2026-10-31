"""Local login, provider-independent sessions and service policy on disposable stores."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import PostgresEndpoints
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from tinypods_api.auth.dependencies import CSRF_COOKIE, SESSION_COOKIE
from tinypods_api.main import create_app
from tinypods_cli.main import app as cli
from tinypods_core.auth import AuthError, AuthService, Credentials, Scope, SessionService
from tinypods_core.auth.passwords import hash_password, verify_password
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database, MigrationManager
from tinypods_core.db.models import AuthSession, LocalCredential, User
from tinypods_core.rate_limit import RateLimiter
from tinypods_core.settings import Settings
from typer.testing import CliRunner

PASSWORD = "a long unique test password"
ROOT = Path(__file__).resolve().parents[1]


def credentials(username: str = "Owner", password: str = PASSWORD) -> Credentials:
    """Create bounded credentials without relying on operator configuration."""
    return Credentials(username=username, password=SecretStr(password))


def local_auth(database: Database, limiter: RateLimiter | None = None) -> AuthService[Credentials]:
    """Select the concrete local adapter at the test composition root."""
    return AuthService(database, LocalPasswordProvider(database, limiter), limiter)


@pytest.fixture
def auth_settings(postgres_endpoints: PostgresEndpoints, valkey_url: str) -> Settings:
    """Migrate a disposable reset schema and isolate Valkey keys for each test."""
    with psycopg.connect(postgres_endpoints.direct, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    settings = Settings(
        database_url=SecretStr(postgres_endpoints.direct),
        valkey_url=SecretStr(valkey_url),
        cache_enabled=False,
        rate_limit_namespace="test:limits:" + uuid4().hex,
    )
    MigrationManager(settings, ROOT / "alembic.ini").upgrade()
    return settings


@pytest.mark.parametrize("pooled", [False, True])
def test_bootstrap_race_and_secret_storage(
    auth_settings: Settings, postgres_endpoints: PostgresEndpoints, pooled: bool
) -> None:
    """Serialize concurrent bootstraps across direct and transaction-pooled connections."""

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
            results = await asyncio.gather(
                LocalPasswordProvider(database).bootstrap(credentials()),
                LocalPasswordProvider(database).bootstrap(credentials("Other")),
                return_exceptions=True,
            )
            assert sum(isinstance(result, UUID) for result in results) == 1
            failures = [result for result in results if isinstance(result, AuthError)]
            assert len(failures) == 1 and failures[0].status == 409
            async with database.session() as session:
                users = (await session.exec(text("SELECT id, username FROM auth_user"))).all()
                passwords = (
                    await session.exec(text("SELECT password_hash FROM auth_local_credential"))
                ).all()
                assert len(users) == len(passwords) == 1
                assert users[0].username in {"owner", "other"}
                assert passwords[0].password_hash.startswith("$argon2id$")
                assert PASSWORD not in passwords[0].password_hash
                assert await verify_password(passwords[0].password_hash, PASSWORD)
                assert passwords[0].password_hash != await hash_password(PASSWORD)
        finally:
            await database.close()

    asyncio.run(scenario())


def test_local_credentials_and_limits(auth_settings: Settings) -> None:
    """Count bad and successful attempts across services without issuing rejected sessions."""

    async def scenario() -> None:
        database = Database(auth_settings)
        limiter = RateLimiter(auth_settings)
        services = [local_auth(database, limiter), local_auth(database, limiter)]
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
            for value in [credentials("missing"), credentials(password="incorrect long password")]:
                with pytest.raises(AuthError) as error:
                    await services[0].login(value)
                assert error.value.status == 401
            for _ in range(4):
                result = await services[1].login(credentials("OWNER"))
                assert result.principal.username == "owner"
            with pytest.raises(AuthError) as error:
                await services[0].login(credentials())
            assert error.value.status == 429 and 1 <= error.value.retry_after <= 60
            # Unknown usernames cannot evade the installation-wide budget or create account keys.
            for _ in range(23):
                with pytest.raises(AuthError) as error:
                    await services[0].login(credentials("missing"))
                assert error.value.status == 401
            with pytest.raises(AuthError) as error:
                await services[1].login(credentials("missing"))
            assert error.value.status == 429
            async with database.session() as session:
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_session"))
                ).scalar_one() == 4
            assert len(await limiter.redis.keys(limiter.namespace + ":*")) == 2
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())


def test_sessions_policy_and_external_identity_separation(auth_settings: Settings) -> None:
    """Authorize password-free identities and revalidate ownership, expiry and revocation."""

    async def scenario() -> None:
        database = Database(auth_settings)
        limiter = RateLimiter(auth_settings)
        auth = local_auth(database, limiter)
        sessions = auth.sessions
        try:
            owner_id = await LocalPasswordProvider(database).bootstrap(credentials())
            owner = await auth.login(credentials())
            external = User(username="external")
            async with database.transaction() as session:
                session.add(external)

            class Provider:
                """Model a trusted future provider using a non-password proof type."""

                async def verify(self, proof: str) -> UUID:
                    """Return the mapped account only for accepted proof."""
                    if proof != "verified provider assertion":
                        raise RuntimeError("provider unavailable")
                    return external.id

            issued = await sessions.login(Provider(), "verified provider assertion")
            async with database.session() as session:
                assert await session.get(LocalCredential, external.id) is None
                stored = await session.get(AuthSession, digest(issued.token))
                assert stored is not None
                assert (
                    stored.token_digest != issued.token and stored.csrf_digest != issued.csrf_token
                )
            assert issued.token not in repr(issued) and issued.csrf_token not in repr(issued)
            assert issued.principal.credential_id not in repr(issued.principal)
            assert await sessions.authenticate(issued.token) == issued.principal
            for scope in (Scope.APP, Scope.CLUSTER, Scope.SECRET):
                assert (
                    await sessions.require_access(issued.principal, scope, owner_id=external.id)
                    == issued.principal
                )
                for principal, resource_owner in (
                    (issued.principal, owner_id),
                    (owner.principal, external.id),
                    (owner.principal, None),
                ):
                    with pytest.raises(AuthError) as error:
                        await sessions.require_access(principal, scope, owner_id=resource_owner)
                    assert error.value.status == 403
            assert (
                await sessions.require_access(owner.principal, Scope.CATALOG_ADMIN)
                == owner.principal
            )
            for principal, scope in (
                (issued.principal, Scope.CATALOG_ADMIN),
                (owner.principal, "unknown"),
                (replace(owner.principal, is_owner=False), Scope.APP),
            ):
                with pytest.raises(AuthError) as error:
                    await sessions.require_access(principal, scope, owner_id=owner_id)
                assert error.value.status == 403
            with pytest.raises(AuthError) as error:
                await sessions.require_access(None, Scope.APP, owner_id=owner_id)
            assert error.value.status == 401
            with pytest.raises(AuthError) as error:
                await LocalPasswordProvider(database, limiter).verify(credentials("external"))
            assert error.value.status == 401
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await sessions.login(Provider(), "bad proof")
            with pytest.raises(AuthError) as error:
                await sessions.authenticate(issued.token, csrf_token="incorrect")
            assert error.value.status == 403
            assert (
                await sessions.authenticate(issued.token, csrf_token=issued.csrf_token)
                == issued.principal
            )
            with pytest.raises(AuthError) as error:
                await sessions.logout(replace(issued.principal, user_id=owner_id))
            assert error.value.status == 403
            await sessions.logout(issued.principal)
            for token in ("", "missing", issued.token):
                with pytest.raises(AuthError) as error:
                    await sessions.authenticate(token)
                assert error.value.status == 401
            with pytest.raises(AuthError):
                await sessions.require_access(issued.principal, Scope.APP, owner_id=external.id)
            expired = await SessionService(database, session_seconds=-1).login(
                Provider(), "verified provider assertion"
            )
            with pytest.raises(AuthError) as error:
                await sessions.authenticate(expired.token)
            assert error.value.status == 401
            # A mapped account must exist even if a trusted adapter returns an unknown ID.
            external.id = uuid4()
            with pytest.raises(AuthError) as error:
                await sessions.login(Provider(), "verified provider assertion")
            assert error.value.status == 401
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())


def test_missing_installation_bad_hash_and_store_failures(auth_settings: Settings) -> None:
    """Fail closed for absent schema/identity, corrupt credentials and absent limiter."""

    async def scenario() -> None:
        database = Database(auth_settings)
        limiter = RateLimiter(auth_settings)
        auth = local_auth(database, limiter)
        try:
            user_id = await LocalPasswordProvider(database).bootstrap(credentials())
            issued = await auth.login(credentials())
            with pytest.raises(AuthError) as error:
                await LocalPasswordProvider(database).verify(credentials())
            assert error.value.status == 503
            with pytest.raises(AuthError) as error:
                await local_auth(database).login(credentials())
            assert error.value.status == 503
            async with database.transaction() as session:
                record = await session.get(LocalCredential, user_id)
                assert record is not None
                record.password_hash = "corrupt encoded password"
                session.add(record)
            with pytest.raises(AuthError) as error:
                await auth.login(credentials())
            assert error.value.status == 401
            async with database.transaction() as session:
                await session.exec(text("DELETE FROM platform_installation"))
            for action in (LocalPasswordProvider(database).bootstrap, auth.login):
                with pytest.raises(AuthError) as error:
                    await action(credentials())
                assert error.value.status == 503
            async with database.transaction() as session:
                await session.exec(text("DROP TABLE auth_session"))
            with pytest.raises(SQLAlchemyError):
                await auth.sessions.authenticate("token")
            with pytest.raises(SQLAlchemyError):
                await auth.sessions.require_access(issued.principal, Scope.APP, owner_id=user_id)
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())


def test_cookie_csrf_logout_and_http_fail_closed(auth_settings: Settings) -> None:
    """Exercise public JSON contracts, browser controls and durable revocation over HTTPS."""

    async def initialize() -> None:
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    app = create_app(auth_settings)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/auth/session").status_code == 401
        payload = {"username": "OWNER", "password": PASSWORD}
        for headers in (
            {},
            {"x-csrf-token": "wrong"},
            {"x-csrf-token": "login", "origin": "https://foreign.example"},
        ):
            response = client.post("/auth/login", json=payload, headers=headers)
            assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
        response = client.post(
            "/auth/login",
            json=payload,
            headers={"x-csrf-token": "login", "origin": "https://testserver"},
        )
        assert response.status_code == 200 and response.json()["username"] == "owner"
        assert "password" not in response.text and "token" not in response.text
        cookies = response.headers.get_list("set-cookie")
        assert all(
            "Secure" in cookie
            and "SameSite=strict" in cookie
            and "Path=/" in cookie
            and "Domain=" not in cookie
            for cookie in cookies
        )
        assert "HttpOnly" in cookies[0] and "HttpOnly" not in cookies[1]
        token = client.cookies[SESSION_COOKIE]
        csrf = client.cookies[CSRF_COOKIE]
        assert client.get("/auth/session").status_code == 200
        for headers in ({}, {"x-csrf-token": "wrong"}, {"x-csrf-token": csrf, "origin": "null"}):
            assert client.post("/auth/logout", headers=headers).status_code == 403
        assert client.get("/auth/session").status_code == 200
        logout = client.post("/auth/logout", headers={"x-csrf-token": csrf})
        assert logout.status_code == 200 and logout.json() == {"logged_out": True}
        assert SESSION_COOKIE not in client.cookies and CSRF_COOKIE not in client.cookies
        assert all("Max-Age=0" in value for value in logout.headers.get_list("set-cookie"))
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/auth/session").status_code == 401
        # Missing limiter configuration cannot disable login throttling.
        app.state.rate_limiter = None
        assert client.post("/auth/login", json=payload).status_code == 503
    with TestClient(create_app(Settings()), base_url="https://testserver") as client:
        assert client.get("/auth/session").status_code == 503
    no_database = Settings(
        valkey_url=auth_settings.valkey_url,
        cache_enabled=False,
        rate_limit_namespace="test:limits:" + uuid4().hex,
    )
    with TestClient(create_app(no_database), base_url="https://testserver") as client:
        assert (
            client.post("/auth/login", json=payload, headers={"x-csrf-token": "login"}).status_code
            == 503
        )
    unavailable = auth_settings.model_copy(
        update={"rate_limit_valkey_url": SecretStr("redis://127.0.0.1:1/0")}
    )
    with TestClient(create_app(unavailable), base_url="https://testserver") as client:
        assert (
            client.post("/auth/login", json=payload, headers={"x-csrf-token": "login"}).status_code
            == 503
        )
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute("DROP TABLE auth_session")
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        assert client.get("/auth/session").status_code == 503


def test_cli_bootstrap_and_validation(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompt for credentials locally; reject repeats and invalid values without leaking secrets."""
    monkeypatch.setenv("TINYPODS_DATABASE_URL", auth_settings.database_url.get_secret_value())
    runner = CliRunner()
    data = "Owner\n" + PASSWORD + "\n" + PASSWORD + "\n"
    first = runner.invoke(cli, ["auth", "bootstrap"], input=data)
    assert first.exit_code == 0 and "owner created" in first.output
    assert PASSWORD not in first.output
    repeated = runner.invoke(cli, ["auth", "bootstrap"], input=data)
    assert repeated.exit_code != 0 and "409" in repeated.output and PASSWORD not in repeated.output
    invalid = runner.invoke(cli, ["auth", "bootstrap"], input="Owner\nshort\nshort\n")
    assert invalid.exit_code != 0 and "15-128" in invalid.output
    for name, password in (("spaces invalid", PASSWORD), ("owner", "short"), ("owner", "x" * 129)):
        with pytest.raises(ValidationError):
            credentials(name, password)
    assert PASSWORD not in repr(credentials())


def test_http_bad_credentials_account_and_endpoint_limits(auth_settings: Settings) -> None:
    """Expose uniform 401, account 429 and endpoint 429 responses without credential leakage."""

    async def initialize() -> None:
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    app = create_app(auth_settings)
    with TestClient(app, base_url="https://testserver") as client:
        headers = {"x-csrf-token": "login"}
        bad = client.post(
            "/auth/login",
            json={"username": "owner", "password": "incorrect password value"},
            headers=headers,
        )
        assert bad.status_code == 401 and "password" not in bad.text
        for _ in range(4):
            assert (
                client.post(
                    "/auth/login", json={"username": "owner", "password": PASSWORD}, headers=headers
                ).status_code
                == 200
            )
        denied = client.post(
            "/auth/login", json={"username": "owner", "password": PASSWORD}, headers=headers
        )
        assert denied.status_code == 429 and int(denied.headers["retry-after"]) > 0
        for _ in range(4):
            assert client.post("/auth/login", json={}, headers=headers).status_code == 422
        denied = client.post("/auth/login", json={}, headers=headers)
        assert denied.status_code == 429 and denied.headers["cache-control"] == "no-store"
        assert denied.json()["request_id"] == denied.headers["x-request-id"]


def test_concurrent_authentication_shares_account_budget(auth_settings: Settings) -> None:
    """Limit simultaneous logins across independent service instances before hashing."""

    async def scenario() -> None:
        database = Database(auth_settings)
        first, second = RateLimiter(auth_settings), RateLimiter(auth_settings)
        one, two = local_auth(database, first), local_auth(database, second)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
            results = await asyncio.gather(
                *[(one if index % 2 else two).login(credentials()) for index in range(10)],
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, AuthError)]
            assert len(failures) == 5 and all(result.status == 429 for result in failures)
            assert sum(not isinstance(result, BaseException) for result in results) == 5
            async with database.session() as session:
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_session"))
                ).scalar_one() == 5
        finally:
            await first.close()
            await second.close()
            await database.close()

    asyncio.run(scenario())


def test_injected_provider_uses_generic_login_without_local_passwords(
    auth_settings: Settings,
) -> None:
    """Replace the selected adapter at composition without changing orchestration or policy."""

    async def scenario() -> None:
        database = Database(auth_settings)
        limiter = RateLimiter(auth_settings)
        identity = User(username="provider-only")

        class ExternalProof:
            """Carry a provider assertion without any username/password fields."""

            def __init__(self, assertion: str) -> None:
                """Retain synthetic proof for the fake adapter."""
                self.assertion = assertion

        class FakeProvider:
            """Model independently supplied authentication code against the generic contract."""

            async def verify(self, proof: ExternalProof) -> UUID:
                """Authenticate only accepted assertions and propagate provider failure."""
                if proof.assertion != "accepted":
                    raise RuntimeError("selected provider unavailable")
                return identity.id

        try:
            async with database.transaction() as session:
                session.add(identity)
            # Only this composition line chooses the adapter; AuthService never imports it.
            auth = AuthService[ExternalProof](database, FakeProvider(), limiter)
            result = await auth.login(ExternalProof("accepted"))
            principal = await auth.sessions.authenticate(result.token, csrf_token=result.csrf_token)
            assert principal.user_id == identity.id
            assert (
                await auth.sessions.require_access(principal, Scope.APP, owner_id=identity.id)
                == principal
            )
            async with database.session() as session:
                assert await session.get(LocalCredential, identity.id) is None
            with pytest.raises(RuntimeError, match="selected provider unavailable"):
                await auth.login(ExternalProof("rejected"))
            async with database.session() as session:
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_session"))
                ).scalar_one() == 1
            await auth.sessions.logout(principal)
            with pytest.raises(AuthError) as error:
                await auth.sessions.authenticate(result.token)
            assert error.value.status == 401
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())
