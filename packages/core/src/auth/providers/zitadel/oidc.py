"""Zitadel Authorization Code + PKCE adapter using Authlib and joserfc validation."""

import asyncio
import re
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx2 as httpx
from authlib.integrations.base_client.errors import OAuthError  # type: ignore[import-untyped]
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from authlib.oidc.core import CodeIDToken  # type: ignore[import-untyped]
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization
from pydantic import BaseModel, ConfigDict, ValidationError
from tinypods_core.auth.contracts import AuthError, VerifiedIdentity
from tinypods_core.settings import Settings


class Discovery(BaseModel):
    """Validate the discovery fields used by this deliberately restricted OIDC adapter."""

    model_config = ConfigDict(strict=True)
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None
    response_types_supported: list[str]
    code_challenge_methods_supported: list[str]
    id_token_signing_alg_values_supported: list[str]
    token_endpoint_auth_methods_supported: list[str]


class ZitadelOIDC:
    """Perform bounded protocol work without retaining provider tokens.

    Discovery must exactly match the configured issuer. Endpoint URLs remain on
    its HTTPS origin, preventing metadata from moving code/verifier/client secrets
    to another host. Each operation owns and closes its native async clients.
    Neither endpoint redirects nor environment proxy settings are trusted.
    """

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Retain validated operator configuration and optional test-only transport injection."""
        if (
            not settings.zitadel_issuer
            or not settings.zitadel_client_id
            or not settings.zitadel_redirect_uri
        ):
            raise AuthError(503)
        self.settings = settings
        self.issuer = settings.zitadel_issuer
        self.client_id = settings.zitadel_client_id
        self.redirect_uri = settings.zitadel_redirect_uri
        self.transport = transport

    def _endpoint(self, value: str) -> str:
        """Accept issuer-origin HTTPS URLs without credentials, query or fragment."""
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            raise AuthError(503) from None
        issuer = urlsplit(self.issuer)
        if (
            parsed.scheme != "https"
            or parsed.netloc != issuer.netloc
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise AuthError(503)
        return value

    async def _json(self, url: str) -> dict[str, Any]:
        """Fetch bounded-time JSON metadata without logging provider-controlled error data."""
        try:
            async with httpx.AsyncClient(
                timeout=2.0,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    raise AuthError(503)
                data = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError(503) from None
        if not isinstance(data, dict):
            raise AuthError(503)
        return data

    async def _discovery(self) -> Discovery:
        """Verify issuer identity, required flow support and every used endpoint."""
        data = await self._json(self.issuer + "/.well-known/openid-configuration")
        try:
            metadata = Discovery.model_validate(data)
        except ValidationError:
            raise AuthError(503) from None
        if (
            metadata.issuer != self.issuer
            or "code" not in metadata.response_types_supported
            or "S256" not in metadata.code_challenge_methods_supported
            or "RS256" not in metadata.id_token_signing_alg_values_supported
            or self.settings.zitadel_token_auth_method
            not in metadata.token_endpoint_auth_methods_supported
        ):
            raise AuthError(503)
        for endpoint in (
            metadata.authorization_endpoint,
            metadata.token_endpoint,
            metadata.jwks_uri,
        ):
            self._endpoint(endpoint)
        if metadata.end_session_endpoint is not None:
            self._endpoint(metadata.end_session_endpoint)
        return metadata

    def _client(self) -> AsyncOAuth2Client:
        """Create a request-owned OAuth client with mandatory PKCE S256 and no refresh behavior."""
        secret = self.settings.zitadel_client_secret
        return AsyncOAuth2Client(
            self.client_id,
            secret.get_secret_value() if secret is not None else None,
            token_endpoint_auth_method=self.settings.zitadel_token_auth_method,
            scope="openid",
            redirect_uri=self.redirect_uri,
            code_challenge_method="S256",
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )

    @staticmethod
    def _proof(nonce: str, verifier: str) -> None:
        """Reject unusable internal login proof rather than allowing omitted nonce/PKCE checks."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce) or not re.fullmatch(
            r"[A-Za-z0-9._~-]{43,128}", verifier
        ):
            raise AuthError(401)

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """Create a fresh authorization URL using server-owned transaction proof."""
        self._proof(nonce, verifier)
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state):
            raise AuthError(401)
        try:
            async with asyncio.timeout(10):
                metadata = await self._discovery()
                async with self._client() as client:
                    url, _ = client.create_authorization_url(
                        metadata.authorization_endpoint,
                        state=state,
                        nonce=nonce,
                        code_verifier=verifier,
                        response_type="code",
                    )
                    return cast(str, url)
        except TimeoutError:
            raise AuthError(503) from None

    async def exchange(self, code: str, nonce: str, verifier: str) -> VerifiedIdentity:
        """Exchange a code and validate its signed ID token before returning identity.

        CodeIDToken enforces essential issuer/subject/audience/timing claims plus
        nonce, authorized party and optional access-token hash. Only RS256 is
        accepted. Tokens and verifiers remain on this server and are discarded.
        Provider/network failures never activate another authentication method.
        """
        self._proof(nonce, verifier)
        if not code or len(code) > 4096:
            raise AuthError(401)
        try:
            async with asyncio.timeout(10):
                metadata = await self._discovery()
                async with self._client() as client:
                    token = await client.fetch_token(
                        metadata.token_endpoint,
                        grant_type="authorization_code",
                        code=code,
                        code_verifier=verifier,
                    )
                encoded = token.get("id_token")
                if not isinstance(encoded, str) or len(encoded) > 65_536:
                    raise AuthError(401)
                keys = await self._json(metadata.jwks_uri)
                decoded = jwt.decode(
                    encoded,
                    KeySet.import_key_set(cast(KeySetSerialization, keys)),
                    algorithms=["RS256"],
                )
                claims = CodeIDToken(
                    decoded.claims,
                    decoded.header,
                    options={
                        "iss": {"essential": True, "value": self.issuer},
                        "sub": {"essential": True},
                        "aud": {"essential": True, "value": self.client_id},
                        "exp": {"essential": True},
                        "iat": {"essential": True},
                        "nonce": {"essential": True, "value": nonce},
                    },
                    params={
                        "nonce": nonce,
                        "client_id": self.client_id,
                        "access_token": token.get("access_token"),
                    },
                )
                claims.validate(leeway=0)
                subject = claims.get("sub")
                if not isinstance(subject, str) or not subject or len(subject) > 255:
                    raise AuthError(401)
                return VerifiedIdentity(self.issuer, subject)
        except (httpx.HTTPError, TimeoutError):
            raise AuthError(503) from None
        except (OAuthError, JoseError, ValueError, KeyError, TypeError):
            raise AuthError(401) from None

    async def logout_url(self) -> str | None:
        """Build optional RP logout navigation using client ID, never an ID token hint.

        Core logout must revoke the TinyPods session first. This navigation does
        not assert that other provider sessions or TinyPods devices were revoked.
        """
        try:
            async with asyncio.timeout(10):
                metadata = await self._discovery()
        except TimeoutError:
            raise AuthError(503) from None
        if metadata.end_session_endpoint is None:
            return None
        parameters = {"client_id": self.client_id}
        if self.settings.zitadel_post_logout_uri is not None:
            parameters["post_logout_redirect_uri"] = self.settings.zitadel_post_logout_uri
        return metadata.end_session_endpoint + "?" + urlencode(parameters)
