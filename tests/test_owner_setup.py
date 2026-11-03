"""Token-gated first-owner setup on isolated stores, preserving initialized installations."""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from test_auth import auth_settings as auth_settings
from test_registration import HEADERS, PASSWORD
from tinypods_api.main import create_app
from tinypods_cli.main import app as cli
from tinypods_core.db import Database
from tinypods_core.db.models import Installation, User
from tinypods_core.settings import Settings
from typer.testing import CliRunner

PATH = "/api/v1/setup"


def body(email: str = "owner@example.com") -> dict[str, str]:
    """Provide valid owner credentials without relying on deployment data."""
    return {"email": email, "password": PASSWORD, "password_confirmation": PASSWORD}


def configured(settings: Settings) -> tuple[Settings, dict[str, str]]:
    """Create an unpredictable test-only operator token."""
    token = secrets.token_urlsafe(32)
    return settings.model_copy(update={"setup_token": SecretStr(token)}), {
        **HEADERS,
        "x-setup-token": token,
    }


def test_setup_requires_operator_token_before_revealing_state(auth_settings: Settings) -> None:
    """Missing, invalid and disabled tokens never reveal a setup form."""
    settings, headers = configured(auth_settings)
    for active, supplied, status in [
        (auth_settings, headers, 404),
        (settings, HEADERS, 403),
        (settings, {**headers, "x-setup-token": "wrong"}, 403),
    ]:
        with TestClient(create_app(active), base_url="https://testserver") as client:
            for response in (
                client.get(PATH, headers=supplied),
                client.post(PATH, headers=supplied, json=body()),
            ):
                assert response.status_code == status
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["referrer-policy"] == "no-referrer"
                assert headers["x-setup-token"] not in response.text


def test_setup_creates_owner_once_with_canonical_email(auth_settings: Settings) -> None:
    """Public registration stays ordinary; setup owns the durable initialization marker."""
    settings, headers = configured(auth_settings)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get(PATH, headers=headers).json() == {"available": True}
        ordinary = client.post(
            "/api/v1/auth/register",
            headers=HEADERS,
            json={
                **body("ordinary@example.com"),
                "username": "ordinary@example.com",
            },
        )
        # The registration contract rejects the setup-only email field.
        assert ordinary.status_code == 422
        ordinary = client.post(
            "/api/v1/auth/register",
            headers=HEADERS,
            json={
                "username": "ordinary@example.com",
                "password": PASSWORD,
                "password_confirmation": PASSWORD,
            },
        )
        assert ordinary.status_code == 201 and not ordinary.json()["user"]["is_owner"]
        response = client.post(PATH, headers=headers, json=body("OWNER@example.com"))
        assert response.status_code == 201
        owner = response.json()["user"]
        assert owner["username"] == "owner@example.com" and owner["is_owner"]
        assert not response.headers.get_list("set-cookie")
        assert client.get(PATH, headers=headers).status_code == 409
        assert (
            client.post(PATH, headers=headers, json=body("second@example.com")).status_code == 409
        )
        signed_in = client.post(
            "/api/v1/auth/login",
            headers=HEADERS,
            json={
                "username": "OWNER@example.com",
                "password": PASSWORD,
            },
        )
        assert signed_in.status_code == 200 and signed_in.json()["user"]["is_owner"]

    async def verify() -> None:
        async with Database(settings) as database, database.session() as session:
            installation = await session.get(Installation, 1)
            assert installation is not None and installation.owner_id == UUID(owner["id"])
            user = await session.get(User, installation.owner_id)
            assert user is not None and user.username == "owner@example.com"

    asyncio.run(verify())


def test_setup_validation_origin_and_conflict(auth_settings: Settings) -> None:
    """Invalid credentials and cross-site attempts cannot initialize the installation."""
    settings, headers = configured(auth_settings)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        for values, field in [
            (body("invalid-email"), "email"),
            ({**body(), "password_confirmation": "another password"}, "password_confirmation"),
            ({**body(), "is_owner": True}, "is_owner"),
        ]:
            response = client.post(PATH, headers=headers, json=values)
            assert response.status_code == 422
            assert any(issue["location"] == ["body", field] for issue in response.json()["issues"])
            assert PASSWORD not in response.text
        for supplied in [
            {"x-setup-token": headers["x-setup-token"]},
            {**headers, "origin": "https://foreign.test"},
        ]:
            assert client.post(PATH, headers=supplied, json=body()).status_code == 403
        assert (
            client.post(
                "/api/v1/auth/register",
                headers=HEADERS,
                json={
                    "username": "owner@example.com",
                    "password": PASSWORD,
                    "password_confirmation": PASSWORD,
                },
            ).status_code
            == 201
        )
        conflict = client.post(PATH, headers=headers, json=body())
        assert conflict.status_code == 409 and conflict.json()["code"] == "setup_conflict"
        assert client.get(PATH, headers=headers).status_code == 200


def test_setup_serializes_two_owner_requests(auth_settings: Settings) -> None:
    """A valid reusable configuration secret can initialize only one owner."""
    settings, headers = configured(auth_settings)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda email: client.post(PATH, headers=headers, json=body(email)),
                    ["first@example.com", "second@example.com"],
                )
            )
        assert sorted(response.status_code for response in responses) == [201, 409]


def test_setup_missing_installation_and_token_cli(auth_settings: Settings) -> None:
    """Missing migrations fail closed; operator-generated secrets are random and bounded."""
    settings, headers = configured(auth_settings)

    async def remove_marker() -> None:
        async with Database(settings) as database, database.transaction() as session:
            await session.exec(text("DELETE FROM platform_installation"))

    asyncio.run(remove_marker())
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get(PATH, headers=headers).status_code == 503
        assert client.post(PATH, headers=headers, json=body()).status_code == 503
        assert (
            client.post(
                "/api/v1/auth/register",
                headers=HEADERS,
                json={
                    "username": "missing-instance",
                    "password": PASSWORD,
                    "password_confirmation": PASSWORD,
                },
            ).status_code
            == 503
        )
    runner = CliRunner()
    values = [runner.invoke(cli, ["auth", "setup-token"]) for _ in range(2)]
    assert all(value.exit_code == 0 and len(value.stdout.strip()) >= 43 for value in values)
    assert values[0].stdout != values[1].stdout
