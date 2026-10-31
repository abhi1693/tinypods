"""Browser refresh-cookie transport and provider-to-server logout HTTP contracts."""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_auth import auth_settings as auth_settings
from test_auth import credentials
from test_oidc_flow import ISSUER, SUBJECT, enabled, state_from
from test_oidc_refresh_sessions import LifecycleProvider
from tinypods_api.auth.contracts import IdentityResponse
from tinypods_api.auth.cookies import cookie_policy
from tinypods_api.auth.dependencies import PrincipalDependency
from tinypods_api.main import create_app
from tinypods_core.auth import AuthError
from tinypods_core.auth.contracts import LogoutNotice
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.settings import Settings


@pytest.mark.parametrize("development", [False, True])
def test_refresh_cookie_transport_and_expired_access_logout(
    auth_settings: Settings, development: bool
) -> None:
    """Renew independently of access cookies while preserving CSRF and environment cookie policy."""
    base = "http://testserver" if development else "https://testserver"
    settings = enabled(
        auth_settings,
        environment="development" if development else "production",
        zitadel_redirect_uri=base + "/auth/providers/zitadel/callback",
        zitadel_refresh_enabled=True,
        zitadel_refresh_encryption_keys=(SecretStr(Fernet.generate_key().decode()),),
    )

    async def initialize() -> None:
        database = Database(settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    app, provider = create_app(settings), LifecycleProvider()
    app.state.browser_login.provider = provider
    app.state.zitadel_sessions.provider = provider
    policy = cookie_policy(settings)

    @app.post("/private-mutation", response_model=IdentityResponse)
    async def mutation(principal: PrincipalDependency) -> IdentityResponse:
        """Model a protected unsafe feature route using the shared principal dependency."""
        return IdentityResponse(
            user_id=principal.user_id, username=principal.username, is_owner=principal.is_owner
        )

    with TestClient(app, base_url=base) as client:

        def login() -> None:
            start = client.post(
                "/auth/providers/zitadel/start", json={}, headers={"x-csrf-token": "login"}
            )
            assert start.status_code == 200
            response = client.get(
                "/auth/providers/zitadel/callback",
                params={"state": state_from(start.json()["authorization_url"]), "code": "accepted"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert client.cookies.get(policy.refresh)
            for header in response.headers.get_list("set-cookie"):
                assert ("Secure" in header) is not development
                assert "SameSite=strict" in header

        login()
        assert client.post("/private-mutation").status_code == 403
        assert (
            client.post(
                "/private-mutation", headers={"x-csrf-token": client.cookies[policy.csrf]}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/private-mutation",
                headers={
                    "x-csrf-token": client.cookies[policy.csrf],
                    "origin": "https://foreign.example",
                },
            ).status_code
            == 403
        )
        advertised = client.get("/auth/providers").json()["data"][1]
        assert advertised["refresh_path"] == "/auth/providers/zitadel/refresh"
        original_refresh, original_csrf = (
            client.cookies[policy.refresh],
            client.cookies[policy.csrf],
        )
        assert client.post(advertised["refresh_path"]).status_code == 403
        assert (
            client.post(
                advertised["refresh_path"],
                headers={"x-csrf-token": original_csrf, "origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        client.cookies.delete(policy.session)
        renewed = client.post(advertised["refresh_path"], headers={"x-csrf-token": original_csrf})
        assert renewed.status_code == 200 and renewed.json()["refresh_expires_at"] is not None
        assert "upstream-private" not in renewed.text and "refresh_token" not in renewed.text
        assert client.cookies[policy.refresh] != original_refresh
        assert client.cookies[policy.csrf] != original_csrf
        assert client.get("/auth/session").status_code == 200
        replay = client.post(
            advertised["refresh_path"],
            headers={
                "x-csrf-token": original_csrf,
                "cookie": policy.refresh + "=" + original_refresh,
            },
        )
        assert replay.status_code == 401
        assert client.get("/auth/session").status_code == 401
        login()
        csrf = client.cookies[policy.csrf]
        client.cookies.delete(policy.session)
        logout = client.post("/auth/logout", headers={"x-csrf-token": csrf})
        assert logout.status_code == 200
        assert policy.refresh not in client.cookies and policy.csrf not in client.cookies
        assert (
            client.post(advertised["refresh_path"], headers={"x-csrf-token": csrf}).status_code
            == 401
        )
        login()
        now = datetime.now(UTC)
        provider.notice = LogoutNotice(
            ISSUER,
            "test-client",
            str(uuid4()),
            now,
            now + timedelta(minutes=2),
            SUBJECT,
            provider.session_id,
        )
        response = client.post(
            "/auth/providers/zitadel/backchannel-logout",
            content=urlencode({"logout_token": "verified", "ignored": "value"}),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200 and response.json()["logged_out"] is True
        assert "set-cookie" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/auth/session").status_code == 401


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"logout_token=invalid", "application/json"),
        (b"\xff", "application/x-www-form-urlencoded"),
        (b"broken", "application/x-www-form-urlencoded"),
        (b"logout_token=one&logout_token=two", "application/x-www-form-urlencoded"),
        (b"ignored=value", "application/x-www-form-urlencoded"),
        (b"x" * 70001, "application/x-www-form-urlencoded"),
        (b"logout_token=invalid", "application/x-www-form-urlencoded"),
    ],
)
def test_backchannel_rejects_invalid_requests_without_browser_credentials(
    auth_settings: Settings, body: bytes, content_type: str
) -> None:
    """Bound parsing and return the fixed logout-protocol error without reflecting input."""
    app = create_app(enabled(auth_settings))
    app.state.zitadel_sessions.provider = LifecycleProvider()
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/auth/providers/zitadel/backchannel-logout",
            content=body,
            headers={"content-type": content_type},
        )
        assert response.status_code == 400 and response.json() == {"error": "invalid_request"}
        assert response.headers["cache-control"] == "no-store"


def test_provider_session_dependencies_and_upstream_logout_failure(auth_settings: Settings) -> None:
    """Return deliberate failures when lifecycle support or provider verification is unavailable."""
    settings = enabled(auth_settings)
    with TestClient(
        create_app(settings.model_copy(update={"database_url": None})),
        base_url="https://testserver",
    ) as client:
        assert client.post("/auth/providers/zitadel/refresh").status_code == 404
    app = create_app(settings)

    async def unavailable(encoded: str) -> LogoutNotice:
        raise AuthError(503)

    app.state.zitadel_sessions.provider.validate_logout_token = unavailable
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/auth/providers/zitadel/backchannel-logout",
            content="logout_token=opaque",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
