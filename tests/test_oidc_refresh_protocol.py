"""Signed refresh/logout protocol profiles and encrypted provider-state boundaries."""

import asyncio
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2 as httpx
import pytest
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr, ValidationError
from test_oidc_protocol import CLIENT, ISSUER, NONCE, STATE, VERIFIER, Issuer
from tinypods_core.auth import AuthError
from tinypods_core.auth.providers.zitadel.encryption import RefreshCipher, RefreshState
from tinypods_core.settings import Settings

EVENT = "http://schemas.openid.net/event/backchannel-logout"


def refresh_settings() -> dict[str, object]:
    """Configure an ephemeral operator key, never operator credentials."""
    return {
        "zitadel_refresh_enabled": True,
        "zitadel_refresh_encryption_keys": (SecretStr(Fernet.generate_key().decode()),),
    }


def test_encrypted_state_binding_tampering_and_key_rotation() -> None:
    """Authenticate ciphertext, bind it to one grant and support staged operator key rotation."""
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old = RefreshCipher(Settings(zitadel_refresh_encryption_keys=(SecretStr(first),)))
    state = RefreshState(
        grant_id=uuid4(),
        refresh_token=SecretStr("private-provider-token"),
        nonce=NONCE,
        auth_time=123.0,
    )
    encoded = old.encrypt(state)
    assert "private-provider-token" not in encoded and "private-provider-token" not in repr(state)
    assert old.decrypt(encoded, state.grant_id) == state
    rotated = RefreshCipher(
        Settings(zitadel_refresh_encryption_keys=(SecretStr(second), SecretStr(first)))
    )
    current = RefreshCipher(Settings(zitadel_refresh_encryption_keys=(SecretStr(second),)))
    assert rotated.decrypt(encoded, state.grant_id) == state
    assert current.decrypt(rotated.encrypt(state), state.grant_id) == state
    for cipher, value, binding in (
        (old, encoded, uuid4()),
        (current, encoded, state.grant_id),
        (old, "tampered", state.grant_id),
        (old, Fernet(first.encode()).encrypt(b"{}").decode(), state.grant_id),
    ):
        with pytest.raises(AuthError) as error:
            cipher.decrypt(value, binding)
        assert error.value.status == 401
    with pytest.raises(AuthError) as error:
        RefreshCipher(Settings())
    assert error.value.status == 503
    for updates in (
        {"zitadel_refresh_enabled": True},
        {"zitadel_refresh_encryption_keys": ("invalid",)},
        {"zitadel_refresh_encryption_keys": ("☃",)},
    ):
        with pytest.raises(ValidationError):
            Settings(**updates)


def test_provider_refresh_rotation_nonce_and_identity_continuity() -> None:
    """Request offline access and validate refresh responses without requiring a new nonce."""
    issuer = Issuer()
    issuer.claims.update(sid="provider-session", auth_time=int(time.time()) - 60)
    received: list[dict[str, list[str]]] = []
    refresh_value: str | None = "provider-refresh-one"

    def respond(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/oauth/v2/token":
            return None
        received.append(parse_qs(request.content.decode()))
        payload = {
            "access_token": "fresh-provider-access",
            "token_type": "Bearer",
            "id_token": jwt.encode(
                {"alg": "RS256", "kid": issuer.key.kid}, issuer.claims, issuer.key
            ),
        }
        if refresh_value is not None:
            payload["refresh_token"] = refresh_value
        return httpx.Response(200, json=payload)

    issuer.override = respond
    adapter = issuer.adapter(**refresh_settings())

    async def scenario() -> None:
        nonlocal refresh_value
        url = await adapter.authorization_url(STATE, NONCE, VERIFIER)
        assert "offline_access" in parse_qs(urlsplit(url).query)["scope"][0].split()
        initial = await adapter.exchange("code-123", NONCE, VERIFIER)
        assert initial.session_id == "provider-session" and initial.refresh_token is not None
        assert initial.refresh_token.get_secret_value() == "provider-refresh-one"
        assert initial.nonce == NONCE
        issuer.claims.pop("nonce")
        refresh_value = "provider-refresh-two"
        renewed = await adapter.refresh(
            initial.refresh_token,
            NONCE,
            subject=initial.subject,
            session_id=initial.session_id,
            auth_time=initial.auth_time,
        )
        assert received[-1]["grant_type"] == ["refresh_token"]
        assert received[-1]["refresh_token"] == ["provider-refresh-one"]
        assert "code" not in received[-1] and "code_verifier" not in received[-1]
        assert (
            renewed.refresh_token is not None
            and renewed.refresh_token.get_secret_value() == refresh_value
        )
        assert "provider-refresh-two" not in repr(renewed)
        refresh_value = None
        retained = await adapter.refresh(
            renewed.refresh_token,
            NONCE,
            subject=initial.subject,
            session_id=None,
            auth_time=initial.auth_time,
        )
        assert retained.refresh_token == renewed.refresh_token
        for key, value in (
            ("sub", "other-user"),
            ("sid", "other-session"),
            ("auth_time", issuer.claims["auth_time"] - 10),
            ("nonce", "wrong-nonce"),
        ):
            original = issuer.claims.get(key)
            issuer.claims[key] = value
            with pytest.raises(AuthError) as error:
                await adapter.refresh(
                    renewed.refresh_token,
                    NONCE,
                    subject=initial.subject,
                    session_id=initial.session_id,
                    auth_time=initial.auth_time,
                )
            assert error.value.status == 401
            if original is None:
                issuer.claims.pop(key)
            else:
                issuer.claims[key] = original
        issuer.claims.pop("sid")
        issuer.claims.pop("auth_time")
        restored = await adapter.refresh(
            renewed.refresh_token,
            NONCE,
            subject=initial.subject,
            session_id=initial.session_id,
            auth_time=initial.auth_time,
        )
        assert restored.session_id == initial.session_id
        assert restored.auth_time == initial.auth_time

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["", 5, "x" * 16385])
def test_invalid_provider_refresh_values(value: object) -> None:
    """Reject unusable refresh material even when the accompanying ID token is valid."""
    issuer = Issuer()

    def respond(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "a",
                    "token_type": "Bearer",
                    "refresh_token": value,
                    "id_token": jwt.encode(
                        {"alg": "RS256", "kid": issuer.key.kid}, issuer.claims, issuer.key
                    ),
                },
            )
        return None

    issuer.override = respond
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter(**refresh_settings()).exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("iat", True),
        ("iat", float("nan")),
        ("auth_time", "invalid"),
        ("auth_time", 9999999999),
        ("sid", ""),
        ("sid", 17),
    ],
)
def test_invalid_signed_session_metadata(field: str, value: object) -> None:
    """Do not accept malformed timing or session IDs from an otherwise signed ID token."""
    issuer = Issuer()
    issuer.claims[field] = value
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == 401


def signed_logout(issuer: Issuer, claims: dict[str, object], *, typ: str = "logout+jwt") -> str:
    """Sign a distinct logout profile with the fixture's actual RSA key."""
    return jwt.encode({"alg": "RS256", "kid": issuer.key.kid, "typ": typ}, claims, issuer.key)


def logout_claims() -> dict[str, object]:
    """Create a recent bounded event with no authentication nonce."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": CLIENT,
        "iat": now,
        "exp": now + 120,
        "jti": str(uuid4()),
        "sid": "provider-session",
        "sub": "subject-123",
        "events": {EVENT: {}},
    }


@pytest.mark.parametrize("selector", ["sub", "sid", "both"])
def test_signed_logout_profiles(selector: str) -> None:
    """Accept subject-wide and session-specific logout with verified issuer and audience."""
    issuer, claims = Issuer(), logout_claims()
    if selector != "both":
        claims.pop("sid" if selector == "sub" else "sub")
    notice = asyncio.run(issuer.adapter().validate_logout_token(signed_logout(issuer, claims)))
    assert notice.issuer == ISSUER and notice.client_id == CLIENT
    assert notice.subject == claims.get("sub") and notice.session_id == claims.get("sid")


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://wrong.example"),
        ("aud", "wrong-client"),
        ("iat", int(time.time()) - 901),
        ("iat", 9_999_999_999),
        ("iat", True),
        ("exp", 1),
        ("exp", float("nan")),
        ("events", {}),
        ("events", {EVENT: []}),
        ("nonce", None),
        ("sub", ""),
        ("sid", 1),
        ("jti", ""),
    ],
)
def test_invalid_signed_logout_claims(field: str, value: object) -> None:
    """Reject forged context, stale events, ID-token confusion and malformed selectors."""
    issuer, claims = Issuer(), logout_claims()
    claims[field] = value
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().validate_logout_token(signed_logout(issuer, claims)))
    assert error.value.status == 400


@pytest.mark.parametrize("missing", ["iss", "aud", "iat", "exp", "jti", "events", "selectors"])
def test_missing_logout_claims(missing: str) -> None:
    """Require the logout profile's essential claims and at least one selector."""
    issuer, claims = Issuer(), logout_claims()
    if missing == "selectors":
        claims.pop("sub")
        claims.pop("sid")
    else:
        claims.pop(missing)
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().validate_logout_token(signed_logout(issuer, claims)))
    assert error.value.status == 400


def test_logout_rejects_untrusted_signature_type_and_oversized_tokens() -> None:
    """Do not accept another key, an authentication JWT type or unbounded token input."""
    issuer, claims = Issuer(), logout_claims()
    wrong = RSAKey.generate_key(auto_kid=True)
    values = [
        "",
        "x" * 65537,
        "malformed",
        signed_logout(issuer, claims, typ="id_token"),
        jwt.encode({"alg": "RS256", "kid": issuer.key.kid}, claims, wrong),
    ]
    for encoded in values:
        with pytest.raises(AuthError) as error:
            asyncio.run(issuer.adapter().validate_logout_token(encoded))
        assert error.value.status == 400


@pytest.mark.parametrize("mode", ["network", "oauth", "timeout"])
def test_refresh_protocol_failures_do_not_expose_provider_details(mode: str) -> None:
    """Fail closed on token-endpoint errors and bounded request cancellation."""
    issuer = Issuer()

    def respond(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/oauth/v2/token":
            return None
        if mode == "network":
            raise httpx.ConnectError("private-provider-message")
        if mode == "timeout":
            raise TimeoutError("private-provider-message")
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "private-provider-message"}
        )

    issuer.override = respond
    with pytest.raises(AuthError) as error:
        asyncio.run(
            issuer.adapter().refresh(
                SecretStr("private-token"),
                NONCE,
                subject="subject-123",
                session_id=None,
                auth_time=None,
            )
        )
    assert error.value.status == (401 if mode == "oauth" else 503)
    assert "private" not in str(error.value)


def test_logout_deadline_returns_storage_safe_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Translate an operation deadline without exposing the signed logout token."""
    issuer = Issuer()
    adapter = issuer.adapter()

    async def timeout() -> None:
        raise TimeoutError

    monkeypatch.setattr(adapter, "_discovery", timeout)
    with pytest.raises(AuthError) as error:
        asyncio.run(adapter.validate_logout_token(signed_logout(issuer, logout_claims())))
    assert error.value.status == 503
