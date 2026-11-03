"""Public registration, safe server validation and enrollment/privilege boundaries."""

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from test_auth import auth_settings as auth_settings
from test_auth import credentials, local_auth
from test_oidc_flow import ISSUER, SUBJECT, enabled, runtime
from tinypods_api.main import create_app
from tinypods_core.auth import AuthError, Scope
from tinypods_core.auth.contracts import VerifiedIdentity
from tinypods_core.auth.federation import IdentityMappings
from tinypods_core.auth.providers.local.provider import REGISTER_GLOBAL, LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.db.models import Installation, LocalCredential, User
from tinypods_core.rate_limit import RateLimiter
from tinypods_core.settings import Settings

PASSWORD = "a unique registration test password"
HEADERS = {"x-csrf-token": "login", "origin": "https://testserver"}
PATH = "/api/v1/auth/register"


def payload(**updates: object) -> dict[str, object]:
    """Build valid test credentials, with intentional invalid overrides."""
    return {
        "username": "new-user",
        "password": PASSWORD,
        "password_confirmation": PASSWORD,
        **updates,
    }


@pytest.mark.parametrize(
    "values,field,message",
    [
        ({}, "username", "Enter a valid value."),
        (
            payload(username="", password="", password_confirmation=""),
            "password",
            "This field is required.",
        ),
        (payload(username="bad name"), "username", "Enter a valid username or email address."),
        (payload(username="u" * 65), "username", "Enter a valid username or email address."),
        (
            payload(password="short-secret", password_confirmation=PASSWORD),
            "password",
            "Use at least 15 characters.",
        ),
        (payload(password="p" * 129), "password", "Use no more than 128 characters."),
        (
            payload(password_confirmation="a different secret password"),
            "password_confirmation",
            "The passwords do not match.",
        ),
        (payload(is_owner=True), "is_owner", "Enter a valid value."),
        (payload(role="owner"), "role", "Enter a valid value."),
    ],
)
def test_registration_uses_safe_server_owned_field_feedback(
    auth_settings: Settings, values: dict[str, object], field: str, message: str
) -> None:
    """Reject invalid values/elevation fields without echoing passwords or inputs."""
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        response = client.post(PATH, json=values, headers=HEADERS)
        assert response.status_code == 422
        data = response.json()
        assert data["message"] == "Check the highlighted fields."
        issue = next(issue for issue in data["issues"] if issue["location"] == ["body", field])
        assert issue["message"] == message
        assert "input" not in response.text and "ctx" not in response.text
        assert PASSWORD not in response.text and "short-secret" not in response.text


def test_registration_creates_only_an_ordinary_account_ready_for_login(
    auth_settings: Settings,
) -> None:
    """Do not issue a session, claim bootstrap ownership, or accept duplicate identities."""
    app = create_app(auth_settings)
    with TestClient(app, base_url="https://testserver") as client:
        discovery = client.get("/api/v1/auth/providers").json()
        assert discovery["registration_allowed"] is True
        assert discovery["data"][0]["registration_path"] == PATH
        response = client.post(PATH, json=payload(username="New-User"), headers=HEADERS)
        assert response.status_code == 201
        user = response.json()["user"]
        assert user["username"] == "new-user" and user["is_owner"] is False
        assert not response.headers.get_list("set-cookie")
        duplicate = client.post(PATH, json=payload(), headers=HEADERS)
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "registration_conflict"
        assert PASSWORD not in duplicate.text
        signed_in = client.post(
            "/api/v1/auth/login",
            json={
                "username": "NEW-USER",
                "password": PASSWORD,
            },
            headers=HEADERS,
        )
        assert signed_in.status_code == 200
        assert signed_in.json()["user"]["is_owner"] is False
        assert client.get("/api/v1/auth/session").status_code == 200

    async def verify() -> None:
        async with Database(auth_settings) as db:
            async with db.session() as session:
                instance = await session.get(Installation, 1)
                assert instance is not None and instance.owner_id is None
                record = await session.get(User, UUID(user["id"]))
                credential = await session.get(LocalCredential, UUID(user["id"]))
                assert record is not None and not record.is_owner
                assert credential is not None and PASSWORD not in credential.password_hash
            limiter = RateLimiter(auth_settings)
            try:
                service = local_auth(db, limiter)
                login = await service.login(credentials("new-user", PASSWORD))
                for scope in [Scope.CATALOG_ADMIN, Scope.CLUSTER, Scope.SECRET]:
                    with pytest.raises(AuthError) as denied:
                        await service.sessions.require_access(
                            login.principal, scope, owner_id=uuid4()
                        )
                    assert denied.value.status == 403
            finally:
                await limiter.close()

    asyncio.run(verify())


def test_closed_registration_is_enforced_but_existing_native_login_remains(
    auth_settings: Settings,
) -> None:
    """Hiding signup matches server refusal without disabling established accounts."""
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        assert client.post(PATH, json=payload(), headers=HEADERS).status_code == 201
    settings = auth_settings.model_copy(update={"auth_registration_enabled": False})
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        discovery = client.get("/api/v1/auth/providers").json()
        assert discovery["registration_allowed"] is False
        assert "registration_path" not in discovery["data"][0]
        assert (
            client.post(PATH, json=payload(username="blocked"), headers=HEADERS).status_code == 403
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={
                    "username": "new-user",
                    "password": PASSWORD,
                },
                headers=HEADERS,
            ).status_code
            == 200
        )


def test_registration_requires_same_trusted_origin_and_csrf(auth_settings: Settings) -> None:
    """Public signup is not a cross-site form endpoint."""
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        for headers in (
            {},
            {"x-csrf-token": "wrong"},
            {**HEADERS, "origin": "https://foreign.test"},
        ):
            assert client.post(PATH, json=payload(), headers=headers).status_code == 403


def test_registration_service_limits_and_races_are_atomic(auth_settings: Settings) -> None:
    """Fail closed without resources; reserve work and resolve concurrent names in PostgreSQL."""

    async def scenario() -> None:
        async with Database(auth_settings) as db:
            with pytest.raises(AuthError) as missing:
                await LocalPasswordProvider(db).register(credentials())
            assert missing.value.status == 503
            limiter = RateLimiter(auth_settings)
            try:
                provider = LocalPasswordProvider(db, limiter)
                results = await asyncio.gather(
                    provider.register(credentials("same", PASSWORD)),
                    provider.register(credentials("SAME", PASSWORD)),
                    return_exceptions=True,
                )
                assert sum(isinstance(result, UUID) for result in results) == 1
                assert (
                    sum(
                        isinstance(result, AuthError) and result.status == 409 for result in results
                    )
                    == 1
                )
                async with db.session() as session:
                    assert len((await session.exec(select(User))).all()) == 1
                    instance = await session.get(Installation, 1)
                    assert instance is not None and instance.owner_id is None
                    subject = str(instance.installation_id)
                for _ in range(30):
                    await limiter.reserve(REGISTER_GLOBAL, subject)
                with pytest.raises(AuthError) as limited:
                    await provider.register(credentials("another", PASSWORD))
                assert limited.value.status == 429
            finally:
                await limiter.close()

    asyncio.run(scenario())


def test_closed_registration_does_not_admit_new_federated_users(auth_settings: Settings) -> None:
    """Existing mappings still resolve; verified new subjects do not bypass enrollment closure."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        async with runtime(settings) as active:
            mappings = IdentityMappings(active.database, settings)
            existing = await mappings.resolve(VerifiedIdentity(ISSUER, SUBJECT))
            settings.auth_registration_enabled = False
            assert await mappings.resolve(VerifiedIdentity(ISSUER, SUBJECT)) == existing
            with pytest.raises(AuthError) as denied:
                await mappings.resolve(VerifiedIdentity(ISSUER, "new-subject"))
            assert denied.value.status == 403
            with pytest.raises(AuthError) as closed:
                await active.browser.start(registration=True)
            assert closed.value.status == 403
            start = await active.browser.start()
            login, _ = await active.browser.finish(
                active.provider.challenges[-1][0],
                start.browser_token,
                "valid-code",
            )
            assert login.principal.user_id == existing and not login.principal.is_owner
            async with active.database.session() as session:
                assert len((await session.exec(select(User))).all()) == 1

    asyncio.run(scenario())
