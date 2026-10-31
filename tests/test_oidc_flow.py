"""Browser OIDC orchestration and attack regressions against disposable durable stores."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from tinypods_api.auth.dependencies import CSRF_COOKIE, SESSION_COOKIE
from tinypods_api.main import create_app
from tinypods_cli.main import app as cli
from tinypods_core.auth import AuthError, Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.federation import BrowserLogin, IdentityMappings
from tinypods_core.auth.oidc import VerifiedIdentity
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database
from tinypods_core.db.models import (
    AuthSession,
    LocalCredential,
    LoginTransaction,
    User,
)
from tinypods_core.rate_limit import RateLimiter, RatePolicy
from tinypods_core.settings import ConfigurationError, Settings, load_settings
from typer.testing import CliRunner

ISSUER = "https://identity.example"
CALLBACK = "https://testserver/auth/providers/zitadel/callback"
SUBJECT = "allowed-subject"
BINDING_COOKIE = "__Host-tinypods-oidc"


def enabled(settings: Settings, **updates: object) -> Settings:
    """Validate a complete explicit provider configuration without operator credentials."""
    return Settings.model_validate(
        {
            **settings.model_dump(),
            "zitadel_enabled": True,
            "zitadel_issuer": ISSUER,
            "zitadel_client_id": "test-client",
            "zitadel_redirect_uri": CALLBACK,
            "zitadel_allowed_subjects": (SUBJECT,),
            **updates,
        }
    )


class FakeProvider:
    """Model the verified protocol boundary; signed-token client tests live separately."""

    def __init__(self) -> None:
        """Retain challenge inputs for assertions without logging or echoing secrets."""
        self.challenges: list[tuple[str, str, str]] = []
        self.exchanges: list[tuple[str, str, str]] = []
        self.start_failure = False

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """Create an opaque challenge and optionally model discovery failure."""
        if self.start_failure:
            raise AuthError(503)
        self.challenges.append((state, nonce, verifier))
        return ISSUER + "/authorize?" + urlencode({"state": state})

    async def exchange(self, code: str, nonce: str, verifier: str) -> VerifiedIdentity:
        """Return only verified identity or a deliberately sanitized protocol failure."""
        self.exchanges.append((code, nonce, verifier))
        if code == "network-failure":
            raise AuthError(503)
        if code == "invalid-code":
            raise AuthError(401)
        if code == "wrong-issuer":
            return VerifiedIdentity("https://untrusted.example", SUBJECT)
        return VerifiedIdentity(ISSUER, "forbidden-subject" if code == "forbidden" else SUBJECT)

    async def logout_url(self) -> str | None:
        """Offer a safe external logout target without an ID-token hint."""
        return ISSUER + "/logout?client_id=test-client"


@dataclass
class Runtime:
    """Own isolated runtime resources for a single async scenario."""

    database: Database
    limiter: RateLimiter
    provider: FakeProvider
    browser: BrowserLogin


@asynccontextmanager
async def runtime(settings: Settings) -> AsyncIterator[Runtime]:
    """Create and close real database/Valkey resources around an injected protocol fake."""
    database = Database(settings)
    limiter = RateLimiter(settings)
    provider = FakeProvider()
    try:
        yield Runtime(
            database, limiter, provider, BrowserLogin(database, settings, provider, limiter)
        )
    finally:
        await limiter.close()
        await database.close()


def state_from(url: str) -> str:
    """Read the public authorization-state parameter from a challenge."""
    return parse_qs(urlsplit(url).query)["state"][0]


def test_server_transactions_rotate_sessions_and_provision_without_passwords(
    auth_settings: Settings,
) -> None:
    """Keep proofs backend-side, rotate the old session and create no privileged local identity."""

    async def scenario() -> None:
        async with runtime(enabled(auth_settings)) as active:
            owner_id = await LocalPasswordProvider(active.database).bootstrap(credentials())
            previous = await active.browser.sessions.issue(owner_id)
            started = await active.browser.start("/apps", previous_token=previous.token)
            state, nonce, verifier = active.provider.challenges[-1]
            assert state == state_from(started.authorization_url)
            assert verifier not in started.authorization_url and started.browser_token not in repr(
                started
            )
            async with active.database.session() as session:
                stored = await session.get(LoginTransaction, digest(state))
                assert stored is not None
                assert (
                    stored.state_digest != state and stored.browser_digest != started.browser_token
                )
                assert stored.verifier == verifier and stored.nonce == nonce
                assert verifier not in repr(stored) and nonce not in repr(stored)
            login, target = await active.browser.finish(
                state, started.browser_token, "accepted", response_issuer=ISSUER
            )
            assert target == "/apps" and login.principal.user_id != owner_id
            assert not login.principal.is_owner
            assert active.provider.exchanges == [("accepted", nonce, verifier)]
            assert await active.browser.sessions.authenticate(login.token) == login.principal
            assert (
                await active.browser.sessions.require_access(
                    login.principal, Scope.APP, owner_id=login.principal.user_id
                )
                == login.principal
            )
            with pytest.raises(AuthError) as error:
                await active.browser.sessions.authenticate(previous.token)
            assert error.value.status == 401
            with pytest.raises(AuthError) as error:
                await active.browser.sessions.require_access(login.principal, Scope.CATALOG_ADMIN)
            assert error.value.status == 403
            async with active.database.session() as session:
                assert await session.get(LocalCredential, login.principal.user_id) is None
                assert await session.get(LoginTransaction, digest(state)) is None
                mapping = (
                    await session.exec(
                        text("SELECT issuer, subject, user_id FROM auth_external_identity")
                    )
                ).one()
                assert mapping.issuer == ISSUER and mapping.subject == SUBJECT
            with pytest.raises(AuthError):
                await active.browser.finish(state, started.browser_token, "accepted")

    asyncio.run(scenario())


def test_browser_binding_and_concurrent_callback_are_single_use(auth_settings: Settings) -> None:
    """Reject wrong browser bindings and let only one callback redeem a challenge."""

    async def scenario() -> None:
        async with runtime(enabled(auth_settings)) as active:
            started = await active.browser.start()
            state = state_from(started.authorization_url)
            for invalid_state, invalid_binding in (
                ("", started.browser_token),
                (state, ""),
                ("x" * 129, started.browser_token),
                (state, "x" * 129),
                (state, "foreign-browser"),
                ("unknown-state", started.browser_token),
            ):
                with pytest.raises(AuthError) as error:
                    await active.browser.finish(invalid_state, invalid_binding, "accepted")
                assert error.value.status == 401
            assert not active.provider.exchanges
            results = await asyncio.gather(
                *[
                    active.browser.finish(state, started.browser_token, "accepted")
                    for _ in range(2)
                ],
                return_exceptions=True,
            )
            assert sum(isinstance(result, tuple) for result in results) == 1
            failures = [result for result in results if isinstance(result, AuthError)]
            assert len(failures) == 1 and failures[0].status == 401
            assert len(active.provider.exchanges) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "attack", ["expired", "configuration", "error", "no-code", "long-code", "issuer"]
)
def test_callback_rejects_invalid_transactions_before_exchange(
    auth_settings: Settings, attack: str
) -> None:
    """Reject stale or malformed callbacks and consume the browser-bound transaction once."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        async with runtime(settings) as active:
            started = await active.browser.start()
            state = state_from(started.authorization_url)
            if attack == "expired":
                async with active.database.transaction() as session:
                    await session.exec(
                        text(
                            "UPDATE auth_login_transaction SET "
                            "expires_at=clock_timestamp()-interval '1 second'"
                        )
                    )
            if attack == "configuration":
                settings.zitadel_client_id = "changed-client"
            kwargs: dict[str, str] = {}
            if attack == "error":
                kwargs["error"] = "provider-sensitive-error-description"
            if attack == "issuer":
                kwargs["response_issuer"] = "https://foreign.example"
            code = (
                None if attack == "no-code" else "x" * 4097 if attack == "long-code" else "accepted"
            )
            with pytest.raises(AuthError) as error:
                await active.browser.finish(state, started.browser_token, code, **kwargs)
            assert error.value.status == 401 and "sensitive" not in str(error.value)
            assert not active.provider.exchanges
            async with active.database.session() as session:
                assert await session.get(LoginTransaction, digest(state)) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,status",
    [("network-failure", 503), ("invalid-code", 401), ("forbidden", 403), ("wrong-issuer", 403)],
)
def test_provider_failure_or_access_denial_never_falls_back(
    auth_settings: Settings, code: str, status: int
) -> None:
    """Do not issue local sessions or identities after protocol failure or access denial."""

    async def scenario() -> None:
        async with runtime(enabled(auth_settings)) as active:
            await LocalPasswordProvider(active.database).bootstrap(credentials())
            started = await active.browser.start()
            state = state_from(started.authorization_url)
            with pytest.raises(AuthError) as error:
                await active.browser.finish(state, started.browser_token, code)
            assert error.value.status == status
            async with active.database.session() as session:
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_session"))
                ).scalar_one() == 0
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_external_identity"))
                ).scalar_one() == 0
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_user"))
                ).scalar_one() == 1
                assert await session.get(LoginTransaction, digest(state)) is None

    asyncio.run(scenario())


def test_identity_admission_linking_and_creation_races(auth_settings: Settings) -> None:
    """Map exact issuer/subject, reject reassignment and never infer privilege from a profile."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        async with runtime(settings) as active:
            owner_id = await LocalPasswordProvider(active.database).bootstrap(credentials())
            mappings = IdentityMappings(active.database, settings)
            for identity in (
                VerifiedIdentity(ISSUER, "not-allowed"),
                VerifiedIdentity("https://other.example", SUBJECT),
            ):
                with pytest.raises(AuthError) as error:
                    await mappings.resolve(identity)
                assert error.value.status == 403
            with pytest.raises(AuthError) as error:
                await mappings.link("not-allowed", owner_id)
            assert error.value.status == 403
            with pytest.raises(AuthError) as error:
                await mappings.link(SUBJECT, uuid4())
            assert error.value.status == 404
            await mappings.link(SUBJECT, owner_id)
            assert await mappings.resolve(VerifiedIdentity(ISSUER, SUBJECT)) == owner_id
            with pytest.raises(AuthError) as error:
                await mappings.link(SUBJECT, owner_id)
            assert error.value.status == 409
            disabled = IdentityMappings(
                active.database, settings.model_copy(update={"zitadel_enabled": False})
            )
            with pytest.raises(AuthError) as error:
                await disabled.link(SUBJECT, owner_id)
            assert error.value.status == 403
            settings.zitadel_allowed_subjects = (SUBJECT, "new-subject")
            created = await asyncio.gather(
                *[mappings.resolve(VerifiedIdentity(ISSUER, "new-subject")) for _ in range(5)]
            )
            assert len(set(created)) == 1 and created[0] != owner_id
            async with active.database.session() as session:
                user = await session.get(User, created[0])
                assert user is not None and not user.is_owner
                assert await session.get(LocalCredential, user.id) is None
            async with active.database.transaction() as session:
                await session.exec(text("DELETE FROM platform_installation"))
            for operation in (
                mappings.resolve(VerifiedIdentity(ISSUER, SUBJECT)),
                mappings.link(SUBJECT, owner_id),
            ):
                with pytest.raises(AuthError) as error:
                    await operation
                assert error.value.status == 503

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example",
        "https://evil.example",
        "/a/../admin",
        "/%2f%2fevil",
        "/\\evil",
        "/path?next=evil",
        "/" + "a" * 300,
        "/" + "a" * 200 + "!",
    ],
)
def test_return_targets_are_local_bounded_paths(auth_settings: Settings, target: str) -> None:
    """Reject protocol-relative, encoded, traversing, unbounded and adversarial return targets."""

    async def scenario() -> None:
        async with runtime(enabled(auth_settings)) as active:
            with pytest.raises(AuthError) as error:
                await active.browser.start(target)
            assert error.value.status == 400 and not active.provider.challenges

    asyncio.run(scenario())


def test_start_outage_missing_resources_and_expired_cleanup(auth_settings: Settings) -> None:
    """Require stores, clean expired proofs and allocate no challenge after discovery failure."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        async with runtime(settings) as active:
            unavailable = BrowserLogin(active.database, settings, active.provider, None)
            with pytest.raises(AuthError) as error:
                await unavailable.start()
            assert error.value.status == 503
            active.provider.start_failure = True
            with pytest.raises(AuthError) as error:
                await active.browser.start()
            assert error.value.status == 503
            async with active.database.session() as session:
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_login_transaction"))
                ).scalar_one() == 0
            active.provider.start_failure = False
            started = await active.browser.start()
            async with active.database.transaction() as session:
                await session.exec(
                    text(
                        "UPDATE auth_login_transaction SET "
                        "expires_at=clock_timestamp()-interval '1 second'"
                    )
                )
            await active.browser.start()
            async with active.database.session() as session:
                assert (
                    await session.get(
                        LoginTransaction, digest(state_from(started.authorization_url))
                    )
                    is None
                )
                assert (
                    await session.exec(text("SELECT count(*) FROM auth_login_transaction"))
                ).scalar_one() == 1
            async with active.database.transaction() as session:
                await session.exec(text("DELETE FROM platform_installation"))
            with pytest.raises(AuthError) as error:
                await active.browser.start()
            assert error.value.status == 503

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "local,external,expected",
    [
        (True, False, ["local"]),
        (True, True, ["local", "zitadel"]),
        (False, True, ["zitadel"]),
        (False, False, []),
    ],
)
def test_public_provider_discovery_is_configured_safe_and_upstream_independent(
    auth_settings: Settings, local: bool, external: bool, expected: list[str]
) -> None:
    """Expose configured login methods without credentials, upstream I/O or implicit fallback."""
    settings = enabled(
        auth_settings,
        auth_local_enabled=local,
        zitadel_enabled=external,
        zitadel_client_secret=SecretStr("never-public-client-secret"),
    )
    app = create_app(settings)
    fake = FakeProvider()
    fake.start_failure = True
    if app.state.browser_login is not None:
        app.state.browser_login.provider = fake
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/auth/providers")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["data"]] == expected
        assert all(item["login_path"].startswith("/auth/") for item in response.json()["data"])
        assert not fake.challenges and not fake.exchanges
        assert all(
            value not in response.text
            for value in (
                "never-public",
                "code_verifier",
                "authorization_url",
                "nonce",
                "state",
                ISSUER,
            )
        )
        assert response.headers["cache-control"] == "no-store"
        if not local:
            assert (
                client.post(
                    "/auth/login",
                    json={"username": "owner", "password": PASSWORD},
                    headers={"x-csrf-token": "login"},
                ).status_code
                == 404
            )
        if external:
            assert (
                client.post(
                    "/auth/providers/zitadel/start", json={}, headers={"x-csrf-token": "login"}
                ).status_code
                == 503
            )
            assert [item["id"] for item in client.get("/auth/providers").json()["data"]] == expected
        else:
            assert (
                client.get("/auth/providers/zitadel/callback?state=unknown&code=code").status_code
                == 404
            )


def test_discovery_defaults_without_database_or_zitadel() -> None:
    """Keep pre-login discovery available without upstream configuration or startup I/O."""
    with TestClient(create_app(Settings()), base_url="https://testserver") as client:
        response = client.get("/auth/providers")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["data"]] == ["local"]


def test_oidc_http_cross_site_callback_multitab_csrf_and_logout(auth_settings: Settings) -> None:
    """Use a Lax correlation cookie while retaining Strict application cookies and rotation."""
    settings = enabled(auth_settings)

    async def initialize() -> str:
        database = Database(settings)
        try:
            owner_id = await LocalPasswordProvider(database).bootstrap(credentials())
            return str(owner_id)
        finally:
            await database.close()

    owner_id = asyncio.run(initialize())
    app = create_app(settings)
    fake = FakeProvider()
    app.state.browser_login.provider = fake
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/auth/login",
            json={"username": "owner", "password": PASSWORD},
            headers={"x-csrf-token": "login"},
        )
        assert login.status_code == 200
        previous = client.cookies[SESSION_COOKIE]
        for headers in (
            {},
            {"x-csrf-token": "wrong"},
            {"x-csrf-token": "login", "origin": "https://foreign.example"},
        ):
            assert (
                client.post("/auth/providers/zitadel/start", json={}, headers=headers).status_code
                == 403
            )
        headers = {"x-csrf-token": "login", "origin": "https://testserver"}
        first = client.post(
            "/auth/providers/zitadel/start", json={"return_to": "/apps"}, headers=headers
        )
        assert first.status_code == 200
        state = state_from(first.json()["authorization_url"])
        binding = client.cookies[BINDING_COOKIE]
        cookie = first.headers["set-cookie"]
        assert (
            "Secure" in cookie
            and "HttpOnly" in cookie
            and "SameSite=lax" in cookie
            and "Max-Age=300" in cookie
        )
        assert "Domain=" not in cookie and fake.challenges[-1][2] not in first.text
        second = client.post(
            "/auth/providers/zitadel/start", json={"return_to": "/clusters"}, headers=headers
        )
        assert second.status_code == 200
        assert client.cookies[BINDING_COOKIE] == binding
        second_state = state_from(second.json()["authorization_url"])
        assert state != second_state and fake.challenges[-1][1:] != fake.challenges[-2][1:]
        # Simulate the browser withholding Strict application cookies on an external top-level GET.
        # The independent Lax binding cookie remains available for correlation.
        client.cookies.delete(SESSION_COOKIE)
        client.cookies.delete(CSRF_COOKIE)
        callback = client.get(
            "/auth/providers/zitadel/callback",
            params={"state": state, "code": "accepted", "iss": ISSUER},
            headers={"sec-fetch-site": "cross-site"},
            follow_redirects=False,
        )
        assert callback.status_code == 303 and callback.headers["location"] == "/apps"
        assert all(
            "SameSite=strict" in value and "Secure" in value
            for value in callback.headers.get_list("set-cookie")
        )
        assert client.cookies[BINDING_COOKIE] == binding
        assert "access_token" not in callback.text and "id_token" not in callback.text
        identity = client.get("/auth/session").json()
        assert identity["user_id"] != owner_id and identity["is_owner"] is False
        # Callback state is single use, while a different outstanding tab still completes.
        assert (
            client.get(
                "/auth/providers/zitadel/callback",
                params={"state": state, "code": "accepted"},
                follow_redirects=False,
            ).status_code
            == 401
        )
        other = client.get(
            "/auth/providers/zitadel/callback",
            params={"state": second_state, "code": "accepted"},
            follow_redirects=False,
        )
        assert other.status_code == 303 and other.headers["location"] == "/clusters"
        csrf = client.cookies[CSRF_COOKIE]
        token = client.cookies[SESSION_COOKIE]
        assert client.post("/auth/providers/zitadel/logout").status_code == 403
        logged_out = client.post("/auth/providers/zitadel/logout", headers={"x-csrf-token": csrf})
        assert logged_out.status_code == 200 and logged_out.json()["logged_out"] is True
        assert logged_out.json()["logout_url"].startswith(ISSUER + "/logout?")
        assert "id_token" not in logged_out.text
        assert SESSION_COOKIE not in client.cookies
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/auth/session").status_code == 401
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert connection.execute(
            "SELECT revoked_at IS NOT NULL FROM auth_session WHERE token_digest=%s",
            (digest(previous),),
        ).fetchone() == (True,)


@pytest.mark.parametrize(
    "query,status",
    [
        ("state=one&state=two&code=accepted", 400),
        ("state=one&code=accepted&code=other", 400),
        ("state=one&code=accepted&error=denied", 400),
        ("state=one&code=accepted", 401),
        ("code=accepted", 422),
    ],
)
def test_http_callback_parameter_attacks_are_redacted(
    auth_settings: Settings, query: str, status: int
) -> None:
    """Reject duplicate/mixed callback parameters without interpreting provider error details."""
    app = create_app(enabled(auth_settings))
    fake = FakeProvider()
    app.state.browser_login.provider = fake
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/auth/providers/zitadel/callback?" + query + "&error_description=sensitive-upstream",
            follow_redirects=False,
        )
        assert response.status_code == status
        assert "sensitive-upstream" not in response.text and "accepted" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert not fake.exchanges


def test_external_http_failure_does_not_activate_disabled_local_login(
    auth_settings: Settings,
) -> None:
    """Treat external failure as a failure even when a valid local password exists."""
    settings = enabled(auth_settings, auth_local_enabled=False)

    async def initialize() -> None:
        database = Database(settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    app = create_app(settings)
    fake = FakeProvider()
    app.state.browser_login.provider = fake
    with TestClient(app, base_url="https://testserver") as client:
        started = client.post(
            "/auth/providers/zitadel/start", json={}, headers={"x-csrf-token": "login"}
        )
        assert started.status_code == 200
        state = state_from(started.json()["authorization_url"])
        failed = client.get(
            "/auth/providers/zitadel/callback",
            params={"state": state, "code": "network-failure"},
            follow_redirects=False,
        )
        assert failed.status_code == 503 and SESSION_COOKIE not in client.cookies
        assert client.get("/auth/providers").json()["data"][0]["id"] == "zitadel"
        assert (
            client.post(
                "/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 404
        )
        # A consumed challenge cannot be retried after an upstream outage.
        assert (
            client.get(
                "/auth/providers/zitadel/callback",
                params={"state": state, "code": "accepted"},
                follow_redirects=False,
            ).status_code
            == 401
        )


def test_cli_identity_linking_is_explicit_and_authorized_by_operator_access(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Link allowlisted subjects through the operator CLI without profile/email matching."""
    settings = enabled(auth_settings)

    async def initialize() -> str:
        database = Database(settings)
        try:
            return str(await LocalPasswordProvider(database).bootstrap(credentials()))
        finally:
            await database.close()

    owner_id = asyncio.run(initialize())
    for name, value in {
        "TINYPODS_DATABASE_URL": settings.database_url.get_secret_value(),
        "TINYPODS_ZITADEL_ENABLED": "true",
        "TINYPODS_ZITADEL_ISSUER": ISSUER,
        "TINYPODS_ZITADEL_CLIENT_ID": "test-client",
        "TINYPODS_ZITADEL_REDIRECT_URI": CALLBACK,
        "TINYPODS_ZITADEL_ALLOWED_SUBJECTS": json.dumps([SUBJECT]),
    }.items():
        monkeypatch.setenv(name, value)
    runner = CliRunner()
    linked = runner.invoke(cli, ["auth", "link", SUBJECT, owner_id])
    assert linked.exit_code == 0 and "identity linked" in linked.output
    repeated = runner.invoke(cli, ["auth", "link", SUBJECT, owner_id])
    assert repeated.exit_code != 0 and "409" in repeated.output
    rejected = runner.invoke(cli, ["auth", "link", "not-allowed", owner_id])
    assert rejected.exit_code != 0 and "403" in rejected.output
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert connection.execute(
            "SELECT user_id FROM auth_external_identity WHERE issuer=%s AND subject=%s",
            (ISSUER, SUBJECT),
        ).fetchone() == (UUID(owner_id),)


@pytest.mark.parametrize(
    "updates",
    [
        {"zitadel_issuer": None},
        {"zitadel_client_id": None},
        {"zitadel_redirect_uri": None},
        {"zitadel_issuer": ISSUER + "/"},
        {"zitadel_redirect_uri": "https://testserver/foreign-callback"},
        {"zitadel_token_auth_method": "client_secret_basic", "zitadel_client_secret": None},
        {"zitadel_issuer": "http://identity.example"},
        {"zitadel_issuer": "https://user:secret@identity.example"},
        {"zitadel_issuer": "https://identity.example:invalid"},
        {"zitadel_issuer": "https://identity.example?q=secret"},
        {"zitadel_issuer": "https://identity.example/#fragment"},
        {"zitadel_issuer": "https://identity.example/\\foreign"},
    ],
)
def test_enabled_oidc_configuration_fails_closed(
    auth_settings: Settings, updates: dict[str, object]
) -> None:
    """Reject incomplete, ambiguous or insecure enabled configuration during validation."""
    with pytest.raises(ValidationError):
        enabled(auth_settings, **updates)


def test_credential_neutral_authorization_does_not_assume_browser_sessions(
    auth_settings: Settings,
) -> None:
    """Validate a future non-browser credential context without implementing API-key issuance."""

    async def scenario() -> None:
        principal = Principal(
            uuid4(), "non-browser", False, "opaque-credential-context", "test-non-browser"
        )

        class Validator:
            """Model an injected non-browser authenticator with explicit denial behavior."""

            def __init__(self) -> None:
                """Retain the accepted context and controllable availability."""
                self.available = True

            async def validate_principal(self, supplied: Principal) -> Principal:
                """Revalidate trusted context or fail closed when unavailable."""
                if not self.available:
                    raise AuthError(503)
                if supplied != principal:
                    raise AuthError(401)
                return principal

        validator = Validator()
        policy = AuthorizationService(validator)
        assert (
            await policy.require_access(principal, Scope.SECRET, owner_id=principal.user_id)
            == principal
        )
        validator.available = False
        with pytest.raises(AuthError) as error:
            await policy.require_access(principal, Scope.APP, owner_id=principal.user_id)
        assert error.value.status == 503
        async with runtime(enabled(auth_settings)) as active:
            with pytest.raises(AuthError) as error:
                await active.browser.sessions.validate_principal(principal)
            assert error.value.status == 401

    asyncio.run(scenario())


def test_disabled_flow_start_budget_and_nonbrowser_context_rejection(
    auth_settings: Settings,
) -> None:
    """Fail closed for disabled or saturated login, without allocating another challenge."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        async with runtime(settings) as active:
            settings.zitadel_enabled = False
            for operation in (
                active.browser.start(),
                active.browser.finish("state", "browser", "code"),
            ):
                with pytest.raises(AuthError) as error:
                    await operation
                assert error.value.status == 404
            settings.zitadel_enabled = True
            malformed_cookie = "x" * 129
            started = await active.browser.start(browser_token=malformed_cookie)
            assert started.browser_token != malformed_cookie and len(started.browser_token) <= 128
            async with active.database.session() as session:
                installation_id = (
                    await session.exec(text("SELECT installation_id FROM platform_installation"))
                ).scalar_one()
            policy = RatePolicy("auth:oidc:start", 30, 60_000)
            for _ in range(29):
                assert (await active.limiter.reserve(policy, str(installation_id))).allowed
            with pytest.raises(AuthError) as error:
                await active.browser.start()
            assert error.value.status == 429 and 1 <= error.value.retry_after <= 60

    asyncio.run(scenario())


def test_enabled_auth_configuration_error_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Translate model-level failures without indexing an empty validation location."""
    monkeypatch.setenv("TINYPODS_ZITADEL_ENABLED", "true")
    monkeypatch.delenv("TINYPODS_ZITADEL_ISSUER", raising=False)
    monkeypatch.delenv("TINYPODS_ZITADEL_CLIENT_ID", raising=False)
    monkeypatch.delenv("TINYPODS_ZITADEL_REDIRECT_URI", raising=False)
    monkeypatch.setenv("TINYPODS_ZITADEL_CLIENT_SECRET", "operator-private-secret")
    with pytest.raises(ConfigurationError) as error:
        load_settings()
    assert "authentication" in str(error.value) and "operator-private-secret" not in str(
        error.value
    )


def test_provider_policy_changes_reject_existing_sessions(auth_settings: Settings) -> None:
    """Revalidate enabled-provider and subject policy on every existing session use."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        async with runtime(settings) as active:
            owner_id = await LocalPasswordProvider(active.database).bootstrap(credentials())
            local = await active.browser.sessions.issue(owner_id)
            started = await active.browser.start()
            external, _ = await active.browser.finish(
                state_from(started.authorization_url), started.browser_token, "accepted"
            )
            async with active.database.session() as session:
                record = await session.get(AuthSession, digest(external.token))
                assert record is not None and record.source_provider == "zitadel"
                assert record.external_issuer == ISSUER and record.external_subject == SUBJECT
            assert await active.browser.sessions.authenticate(external.token) == external.principal
            changes = (
                ("zitadel_allowed_subjects", ()),
                ("zitadel_issuer", "https://different-issuer.example"),
                ("zitadel_enabled", False),
            )
            for field, rejected in changes:
                original = getattr(settings, field)
                setattr(settings, field, rejected)
                with pytest.raises(AuthError) as error:
                    await active.browser.sessions.authenticate(external.token)
                assert error.value.status == 401
                setattr(settings, field, original)
                assert (
                    await active.browser.sessions.authenticate(external.token) == external.principal
                )
            settings.auth_local_enabled = False
            with pytest.raises(AuthError) as error:
                await active.browser.sessions.authenticate(local.token)
            assert error.value.status == 401
            assert await active.browser.sessions.authenticate(external.token) == external.principal
            settings.auth_local_enabled = True
            async with active.database.transaction() as session:
                await session.exec(
                    text(
                        "UPDATE auth_session SET source_provider='unknown' "
                        "WHERE token_digest=:digest"
                    ),
                    params={"digest": digest(external.token)},
                )
            with pytest.raises(AuthError) as error:
                await active.browser.sessions.authenticate(external.token)
            assert error.value.status == 401

    asyncio.run(scenario())


def test_rotation_missing_prior_session_and_secret_change_invalidate_transaction(
    auth_settings: Settings,
) -> None:
    """Allow an unrecognized old cookie to rotate safely and invalidate changed client secrets."""

    async def scenario() -> None:
        settings = enabled(auth_settings, zitadel_client_secret=SecretStr("original-client-secret"))
        async with runtime(settings) as active:
            started = await active.browser.start(previous_token="invalid-prior-session")
            external, _ = await active.browser.finish(
                state_from(started.authorization_url), started.browser_token, "accepted"
            )
            assert await active.browser.sessions.authenticate(external.token) == external.principal
            started = await active.browser.start()
            settings.zitadel_client_secret = SecretStr("changed-client-secret")
            with pytest.raises(AuthError) as error:
                await active.browser.finish(
                    state_from(started.authorization_url), started.browser_token, "accepted"
                )
            assert error.value.status == 401

    asyncio.run(scenario())


def test_enabled_browser_service_absence_and_logout_upstream_failure(
    auth_settings: Settings,
) -> None:
    """Reject missing service composition and clear revoked cookies when provider logout fails."""
    settings = enabled(auth_settings)
    app = create_app(settings)
    fake = FakeProvider()
    app.state.browser_login.provider = fake
    with TestClient(app, base_url="https://testserver") as client:
        started = client.post(
            "/auth/providers/zitadel/start", json={}, headers={"x-csrf-token": "login"}
        )
        assert started.status_code == 200
        state = state_from(started.json()["authorization_url"])
        assert (
            client.get(
                "/auth/providers/zitadel/callback",
                params={"state": state, "code": "accepted"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        token, csrf = client.cookies[SESSION_COOKIE], client.cookies[CSRF_COOKIE]

        async def unavailable_logout() -> str | None:
            """Model unavailable provider discovery after local revocation committed."""
            raise AuthError(503)

        fake.logout_url = unavailable_logout
        failed = client.post("/auth/providers/zitadel/logout", headers={"x-csrf-token": csrf})
        assert failed.status_code == 503
        assert SESSION_COOKIE not in client.cookies and CSRF_COOKIE not in client.cookies
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/auth/session").status_code == 401
        app.state.browser_login = None
        assert (
            client.post(
                "/auth/providers/zitadel/start", json={}, headers={"x-csrf-token": "login"}
            ).status_code
            == 404
        )
