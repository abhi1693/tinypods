"""Exercise the production Authlib code path against a deterministic standards-valid issuer."""

import asyncio
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from authlib.oidc.core.util import create_half_hash
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey
from pydantic import SecretStr
from tinypods_core.auth import AuthError
from tinypods_core.auth.providers.zitadel.oidc import VerifiedIdentity, ZitadelOIDC
from tinypods_core.settings import Settings

ISSUER = "https://issuer.example"
CLIENT = "client-id"
CALLBACK = "https://tinypods.example/auth/providers/zitadel/callback"
STATE = "s" * 43
NONCE = "n" * 43
# RFC 7636 section 4.2 example, independent of Authlib's challenge implementation.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


class Issuer:
    """Serve discovery, JWKS and signed ID tokens while observing server-only code exchange."""

    def __init__(self) -> None:
        """Generate an ephemeral issuer key and a valid code-flow token."""
        self.key = RSAKey.generate_key(auto_kid=True)
        now = int(time.time())
        self.claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": "subject-123",
            "aud": CLIENT,
            "iat": now,
            "exp": now + 300,
            "nonce": NONCE,
            "email": "owner@example.org",
            "roles": ["admin"],
        }
        self.metadata: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/oauth/v2/authorize",
            "token_endpoint": ISSUER + "/oauth/v2/token",
            "jwks_uri": ISSUER + "/oauth/v2/keys",
            "end_session_endpoint": ISSUER + "/oidc/v1/end_session",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        }
        self.exchange: httpx.Request | None = None
        self.override: Callable[[httpx.Request], httpx.Response | None] | None = None
        self.encoded: str | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Respond over the injected HTTP transport; never bypass production protocol code."""
        if self.override is not None:
            response = self.override(request)
            if response is not None:
                return response
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.metadata)
        if request.url.path == "/oauth/v2/keys":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        assert request.url.path == "/oauth/v2/token"
        self.exchange = request
        body = parse_qs(request.content.decode())
        assert request.method == "POST"
        assert body["code"] == ["code-123"]
        assert body["grant_type"] == ["authorization_code"]
        assert body["redirect_uri"] == [CALLBACK]
        assert body["code_verifier"] == [VERIFIER]
        encoded = self.encoded or jwt.encode(
            {"alg": "RS256", "kid": self.key.kid},
            self.claims,
            self.key,
        )
        return httpx.Response(
            200,
            json={
                "id_token": encoded,
                "access_token": "server-only-access-token",
                "token_type": "Bearer",
            },
        )

    def adapter(self, **overrides: object) -> ZitadelOIDC:
        """Compose a production adapter with the deterministic issuer transport."""
        options: dict[str, Any] = {
            "zitadel_enabled": True,
            "zitadel_issuer": ISSUER,
            "zitadel_client_id": CLIENT,
            "zitadel_redirect_uri": CALLBACK,
            **overrides,
        }
        return ZitadelOIDC(Settings(**options), transport=httpx.MockTransport(self.handle))


def test_authorization_pkce_exchange_claims_and_logout() -> None:
    """Validate the RFC challenge vector, real signature/claims and no token exposure."""
    issuer = Issuer()
    issuer.claims["at_hash"] = create_half_hash("server-only-access-token", "RS256").decode()

    async def scenario() -> None:
        adapter = issuer.adapter(zitadel_post_logout_uri="https://tinypods.example/logged-out")
        url = await adapter.authorization_url(STATE, NONCE, VERIFIER)
        parameters = parse_qs(urlsplit(url).query)
        assert parameters == {
            "client_id": [CLIENT],
            "redirect_uri": [CALLBACK],
            "scope": ["openid"],
            "response_type": ["code"],
            "state": [STATE],
            "nonce": [NONCE],
            "code_challenge": [CHALLENGE],
            "code_challenge_method": ["S256"],
        }
        result = await adapter.exchange("code-123", NONCE, VERIFIER)
        assert result == VerifiedIdentity(ISSUER, "subject-123")
        assert "admin" not in repr(result) and "token" not in repr(result)
        assert issuer.exchange is not None
        assert "authorization" not in issuer.exchange.headers
        assert parse_qs(issuer.exchange.content.decode())["client_id"] == [CLIENT]
        logout = await adapter.logout_url()
        assert (
            logout
            == ISSUER
            + "/oidc/v1/end_session?client_id=client-id"
            + "&post_logout_redirect_uri=https%3A%2F%2Ftinypods.example%2Flogged-out"
        )
        assert "token" not in logout
        issuer.metadata.pop("end_session_endpoint")
        assert await adapter.logout_url() is None

    asyncio.run(scenario())


def test_confidential_client_and_optional_logout_uri() -> None:
    """Use basic client authentication plus PKCE without returning secrets in URLs."""
    issuer = Issuer()

    async def scenario() -> None:
        adapter = issuer.adapter(
            zitadel_token_auth_method="client_secret_basic",
            zitadel_client_secret=SecretStr("server-client-secret"),
        )
        result = await adapter.exchange("code-123", NONCE, VERIFIER)
        assert result.subject == "subject-123"
        assert issuer.exchange is not None
        assert issuer.exchange.headers["authorization"].startswith("Basic ")
        assert "client_secret" not in parse_qs(issuer.exchange.content.decode())
        assert await adapter.logout_url() == ISSUER + "/oidc/v1/end_session?client_id=client-id"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://other-issuer.example"),
        ("aud", "wrong-client"),
        ("aud", [CLIENT, "other-client"]),
        ("azp", "wrong-client"),
        ("nonce", "wrong-nonce"),
        ("exp", 1),
        ("iat", 9_999_999_999),
        ("sub", ""),
        ("sub", 123),
        ("sub", "s" * 256),
        ("at_hash", "wrong"),
    ],
)
def test_invalid_signed_identity_claims_are_rejected(field: str, value: object) -> None:
    """Reject invalid claims even though the token has a valid trusted issuer signature."""
    issuer = Issuer()
    issuer.claims[field] = value
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == 401


@pytest.mark.parametrize("field", ["iss", "sub", "aud", "exp", "iat", "nonce"])
def test_missing_required_claims(field: str) -> None:
    """Require all OIDC code-flow identity and request-correlation claims."""
    issuer = Issuer()
    issuer.claims.pop(field)
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == 401


def test_multi_audience_and_nonce_supported_cannot_disable_validation() -> None:
    """Accept a matching azp but never let a signed token disable nonce verification."""
    issuer = Issuer()
    issuer.claims.update(aud=[CLIENT, "other-client"], azp=CLIENT)
    assert (
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER)).subject == "subject-123"
    )
    issuer.claims.update(nonce_supported=False, nonce="wrong-nonce")
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == 401


@pytest.mark.parametrize("mode", ["signature", "hs256", "none", "malformed", "oversized"])
def test_untrusted_signatures_and_algorithms(mode: str) -> None:
    """Reject forged signatures, symmetric/unsigned algorithms and unusable ID token values."""
    issuer = Issuer()
    if mode == "signature":
        other = RSAKey.generate_key()
        issuer.encoded = jwt.encode({"alg": "RS256", "kid": issuer.key.kid}, issuer.claims, other)
    elif mode == "hs256":
        issuer.encoded = jwt.encode(
            {"alg": "HS256", "kid": issuer.key.kid}, issuer.claims, OctKey.generate_key(256)
        )
    elif mode == "none":
        issuer.encoded = "eyJhbGciOiJub25lIn0.e30."
    elif mode == "malformed":
        issuer.encoded = "not-a-jwt"
    else:
        issuer.encoded = "x" * 65_537
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://other.example"),
        ("response_types_supported", ["token"]),
        ("code_challenge_methods_supported", ["plain"]),
        ("id_token_signing_alg_values_supported", ["HS256"]),
        ("token_endpoint_auth_methods_supported", ["client_secret_post"]),
        ("token_endpoint", "https://other.example/token"),
        ("authorization_endpoint", "http://issuer.example/authorize"),
        ("jwks_uri", "https://issuer.example/keys?injected=1"),
        ("end_session_endpoint", "https://issuer.example/logout#fragment"),
        ("token_endpoint", "https://issuer.example:bad/token"),
        ("token_endpoint", "https://user@issuer.example/token"),
        ("token_endpoint", "https://[invalid/token"),
        ("token_endpoint", 123),
    ],
)
def test_discovery_cannot_move_credentials_or_downgrade_flow(field: str, value: object) -> None:
    """Require exact issuer, supported flow/algorithm and same-origin HTTPS endpoints."""
    issuer = Issuer()
    issuer.metadata[field] = value
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().authorization_url(STATE, NONCE, VERIFIER))
    assert error.value.status == 503


@pytest.mark.parametrize("mode", ["redirect", "json", "shape", "oversized", "network", "timeout"])
def test_discovery_failures_are_redacted_and_fail_closed(mode: str) -> None:
    """Reject redirects, malformed metadata, unavailable networks and bounded-time failures."""
    issuer = Issuer()

    def override(request: httpx.Request) -> httpx.Response | None:
        """Inject a deliberate provider response or transport failure."""
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://attacker.example"})
        if mode == "json":
            return httpx.Response(200, text="sensitive invalid json")
        if mode == "shape":
            return httpx.Response(200, json=[])
        if mode == "oversized":
            return httpx.Response(200, content=b"x" * 1_000_001)
        if mode == "network":
            raise httpx.ConnectError("sensitive network error")
        raise TimeoutError("sensitive timeout")

    issuer.override = override
    for operation in (
        lambda adapter: adapter.authorization_url(STATE, NONCE, VERIFIER),
        lambda adapter: adapter.exchange("code-123", NONCE, VERIFIER),
        lambda adapter: adapter.logout_url(),
    ):
        with pytest.raises(AuthError) as error:
            asyncio.run(operation(issuer.adapter()))
        assert error.value.status == 503 and "sensitive" not in str(error.value)


@pytest.mark.parametrize("mode", ["oauth", "server", "missing", "type", "list", "jwks"])
def test_code_exchange_and_jwks_failures(mode: str) -> None:
    """Translate deliberate token/provider failures without exposing any response body."""
    issuer = Issuer()

    def override(request: httpx.Request) -> httpx.Response | None:
        """Provide a deliberately unusable token response or key set."""
        if request.url.path == "/oauth/v2/keys" and mode == "jwks":
            return httpx.Response(200, json={"keys": []})
        if request.url.path != "/oauth/v2/token":
            return None
        if mode == "oauth":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "sensitive"}
            )
        if mode == "server":
            return httpx.Response(500, text="sensitive")
        if mode == "missing":
            return httpx.Response(200, json={"access_token": "sensitive"})
        if mode == "type":
            return httpx.Response(200, json={"id_token": 123})
        if mode == "list":
            return httpx.Response(200, json=[])
        return None

    issuer.override = override
    with pytest.raises(AuthError) as error:
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))
    assert error.value.status == (503 if mode == "server" else 401)
    assert "sensitive" not in str(error.value)


def test_missing_config_bad_internal_proof_and_unexpected_failure() -> None:
    """Reject unusable configuration/proof and preserve cancellation/programming errors."""
    with pytest.raises(AuthError) as error:
        ZitadelOIDC(Settings())
    assert error.value.status == 503
    issuer = Issuer()
    for state, nonce, verifier in (
        ("bad", NONCE, VERIFIER),
        (STATE, "bad", VERIFIER),
        (STATE, NONCE, "bad"),
    ):
        with pytest.raises(AuthError) as error:
            asyncio.run(issuer.adapter().authorization_url(state, nonce, verifier))
        assert error.value.status == 401
    for code in ("", "x" * 4097):
        with pytest.raises(AuthError) as error:
            asyncio.run(issuer.adapter().exchange(code, NONCE, VERIFIER))
        assert error.value.status == 401

    def bug(request: httpx.Request) -> httpx.Response | None:
        """Expose a programming failure rather than converting it into an identity."""
        raise RuntimeError("bug")

    issuer.override = bug
    with pytest.raises(RuntimeError, match="bug"):
        asyncio.run(issuer.adapter().exchange("code-123", NONCE, VERIFIER))

    def cancelled(request: httpx.Request) -> httpx.Response | None:
        """Expose cancellation rather than converting it into an authentication failure."""
        raise asyncio.CancelledError

    issuer.override = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(issuer.adapter().authorization_url(STATE, NONCE, VERIFIER))


@pytest.mark.parametrize("upstream", [None, "upstream-idp"])
def test_organization_selection_and_optional_upstream_provider(upstream: str | None) -> None:
    """Select the organization without requiring an upstream IdP or exposing its configuration."""
    issuer = Issuer()
    organization = "organization-id"
    issuer.claims["urn:zitadel:iam:user:resourceowner:id"] = organization

    async def scenario() -> None:
        adapter = issuer.adapter(zitadel_organization_id=organization)
        target = await adapter.authorization_url(
            STATE, NONCE, VERIFIER, identity_provider_id=upstream
        )
        scopes = parse_qs(urlsplit(target).query)["scope"][0].split()
        assert "urn:zitadel:iam:org:id:" + organization in scopes
        assert any(scope.startswith("urn:zitadel:iam:org:idp:id:") for scope in scopes) is (
            upstream is not None
        )
        assert await adapter.exchange("code-123", NONCE, VERIFIER) == VerifiedIdentity(
            ISSUER, "subject-123"
        )
        issuer.claims["urn:zitadel:iam:user:resourceowner:id"] = "another-organization"
        with pytest.raises(AuthError) as error:
            await adapter.exchange("code-123", NONCE, VERIFIER)
        assert error.value.status == 403
        issuer.claims.pop("urn:zitadel:iam:user:resourceowner:id")
        with pytest.raises(AuthError) as error:
            await adapter.exchange("code-123", NONCE, VERIFIER)
        assert error.value.status == 403

    asyncio.run(scenario())


def test_upstream_selection_requires_an_organization() -> None:
    """Reject incomplete IdP selection and scope-injection characters in operator IDs."""
    from pydantic import ValidationError

    for settings in (
        {"zitadel_identity_providers": [{"id": "selected", "type": "google", "label": "Google"}]},
        {"zitadel_organization_id": "invalid scope"},
        {
            "zitadel_identity_providers": [
                {"id": "id with spaces", "type": "google", "label": "Google"}
            ],
            "zitadel_organization_id": "organization",
        },
    ):
        with pytest.raises(ValidationError):
            Settings(**settings)


def test_organization_domain_scope_and_signed_claim_validation() -> None:
    """Select by domain and reject missing or mismatched signed organization claims."""
    issuer = Issuer()
    domain = "example.org"
    issuer.claims["urn:zitadel:iam:user:resourceowner:primary_domain"] = domain

    async def scenario() -> None:
        adapter = issuer.adapter(zitadel_organization_domain="EXAMPLE.ORG")
        target = await adapter.authorization_url(STATE, NONCE, VERIFIER)
        scopes = parse_qs(urlsplit(target).query)["scope"][0].split()
        assert "urn:zitadel:iam:org:domain:primary:" + domain in scopes
        assert "urn:zitadel:iam:user:resourceowner" in scopes
        assert not any(scope.startswith("urn:zitadel:iam:org:id:") for scope in scopes)
        assert await adapter.exchange("code-123", NONCE, VERIFIER) == VerifiedIdentity(
            ISSUER, "subject-123"
        )
        issuer.claims["urn:zitadel:iam:user:resourceowner:primary_domain"] = "other.org"
        with pytest.raises(AuthError) as error:
            await adapter.exchange("code-123", NONCE, VERIFIER)
        assert error.value.status == 403
        issuer.claims.pop("urn:zitadel:iam:user:resourceowner:primary_domain")
        with pytest.raises(AuthError) as error:
            await adapter.exchange("code-123", NONCE, VERIFIER)
        assert error.value.status == 403

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "updates",
    [
        {"zitadel_organization_id": "org", "zitadel_organization_domain": "example.org"},
        {"zitadel_organization_domain": "https://example.org"},
        {"zitadel_organization_domain": "example.org/path"},
        {"zitadel_organization_domain": "example.org extra-scope"},
        {"zitadel_organization_domain": "-example.org"},
    ],
)
def test_organization_selectors_are_exclusive_and_domains_are_dns_names(
    updates: dict[str, object],
) -> None:
    """Reject two selectors and URL or scope injection in a domain selector."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(**updates)


def test_upstream_provider_can_use_organization_domain_instead_of_id() -> None:
    """Allow domain-based organizations to configure upstream sign-in choices."""
    settings = Settings(
        zitadel_organization_domain="example.org",
        zitadel_identity_providers=[{"id": "google-id", "type": "google", "label": "Google"}],
    )
    assert settings.zitadel_organization_id is None
    assert settings.zitadel_identity_providers[0].id == "google-id"
    assert Settings(zitadel_organization_domain=None).zitadel_organization_domain is None
