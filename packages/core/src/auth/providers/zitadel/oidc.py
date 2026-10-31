"""Zitadel Authorization Code + PKCE adapter using Authlib and joserfc validation."""

import asyncio
import logging
import math
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx2 as httpx
from authlib.integrations.base_client.errors import OAuthError  # type: ignore[import-untyped]
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from authlib.oidc.core import CodeIDToken  # type: ignore[import-untyped]
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization
from joserfc.jwt import JWTClaimsRegistry
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from tinypods_core.auth.contracts import AuthError, LogoutNotice, VerifiedIdentity
from tinypods_core.settings import Settings

logger = logging.getLogger(__name__)


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

    def _scope(self, identity_provider_id: str | None = None) -> str:
        """Select the configured organization and optionally an upstream identity provider."""
        scopes = ["openid"]
        if self.settings.zitadel_refresh_enabled:
            scopes.append("offline_access")
        if self.settings.zitadel_organization_id is not None:
            scopes.append("urn:zitadel:iam:org:id:" + self.settings.zitadel_organization_id)
        if self.settings.zitadel_organization_domain is not None:
            scopes.append(
                "urn:zitadel:iam:org:domain:primary:" + self.settings.zitadel_organization_domain
            )
            scopes.append("urn:zitadel:iam:user:resourceowner")
        if identity_provider_id is not None:
            scopes.append("urn:zitadel:iam:org:idp:id:" + identity_provider_id)
        return " ".join(scopes)

    def _client(self, identity_provider_id: str | None = None) -> AsyncOAuth2Client:
        """Create a request-owned OAuth client with mandatory PKCE S256 and no refresh behavior."""
        secret = self.settings.zitadel_client_secret
        return AsyncOAuth2Client(
            self.client_id,
            secret.get_secret_value() if secret is not None else None,
            token_endpoint_auth_method=self.settings.zitadel_token_auth_method,
            scope=self._scope(identity_provider_id),
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

    async def authorization_url(
        self, state: str, nonce: str, verifier: str, *, identity_provider_id: str | None = None
    ) -> str:
        """Create a fresh authorization URL using server-owned transaction proof."""
        self._proof(nonce, verifier)
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state):
            raise AuthError(401)
        try:
            async with asyncio.timeout(10):
                metadata = await self._discovery()
                async with self._client(identity_provider_id) as client:
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
                return await self._identity(token, nonce, metadata, require_nonce=True)
        except (httpx.HTTPError, TimeoutError):
            raise AuthError(503) from None
        except (OAuthError, JoseError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "Zitadel code exchange or token validation rejected",
                extra={"failure_type": type(exc).__name__},
            )
            raise AuthError(401) from None

    @staticmethod
    def _number(value: object) -> float:
        """Reject non-finite or non-numeric signed timing claims."""
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise AuthError(401)
        return float(value)

    @staticmethod
    def _identifier(value: object) -> str | None:
        """Validate an optional opaque provider session identifier."""
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 255:
            raise AuthError(401)
        return value

    async def _identity(
        self, token: dict[str, Any], nonce: str, metadata: Discovery, *, require_nonce: bool
    ) -> VerifiedIdentity:
        """Validate a signed initial or refreshed ID token before returning server-only state."""
        encoded = token.get("id_token")
        if not isinstance(encoded, str) or len(encoded) > 65_536:
            raise AuthError(401)
        keys = await self._json(metadata.jwks_uri)
        decoded = jwt.decode(
            encoded, KeySet.import_key_set(cast(KeySetSerialization, keys)), algorithms=["RS256"]
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
                "nonce": {"essential": require_nonce},
            },
            params={
                "nonce": nonce if require_nonce else None,
                "client_id": self.client_id,
                "access_token": token.get("access_token"),
            },
        )
        claims.validate(leeway=0)
        if "nonce" in claims and claims["nonce"] != nonce:
            raise AuthError(401)
        organization = self.settings.zitadel_organization_id
        if (
            organization is not None
            and claims.get("urn:zitadel:iam:user:resourceowner:id") != organization
        ):
            raise AuthError(403)
        domain = self.settings.zitadel_organization_domain
        if (
            domain is not None
            and claims.get("urn:zitadel:iam:user:resourceowner:primary_domain") != domain
        ):
            raise AuthError(403)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise AuthError(401)
        issued_at = self._number(claims["iat"])
        auth_time = self._number(claims["auth_time"]) if "auth_time" in claims else None
        if auth_time is not None and auth_time > issued_at:
            raise AuthError(401)
        refresh: SecretStr | None = None
        if self.settings.zitadel_refresh_enabled and "refresh_token" in token:
            value = token["refresh_token"]
            if not isinstance(value, str) or not value or len(value) > 16_384:
                raise AuthError(401)
            refresh = SecretStr(value)
        return VerifiedIdentity(
            self.issuer,
            subject,
            self._identifier(claims.get("sid")),
            refresh,
            nonce,
            auth_time,
            issued_at,
        )

    async def refresh(
        self,
        refresh_token: SecretStr,
        nonce: str,
        *,
        subject: str,
        session_id: str | None,
        auth_time: float | None,
    ) -> VerifiedIdentity:
        """Refresh through the provider and validate identity continuity before rotation."""
        try:
            async with asyncio.timeout(10):
                metadata = await self._discovery()
                async with self._client() as client:
                    token = await client.fetch_token(
                        metadata.token_endpoint,
                        grant_type="refresh_token",
                        refresh_token=refresh_token.get_secret_value(),
                    )
                identity = await self._identity(token, nonce, metadata, require_nonce=False)
                if identity.subject != subject:
                    raise AuthError(401)
                if (
                    identity.session_id is not None
                    and session_id is not None
                    and identity.session_id != session_id
                ):
                    raise AuthError(401)
                if identity.auth_time is not None and identity.auth_time != auth_time:
                    raise AuthError(401)
                return replace(
                    identity,
                    refresh_token=identity.refresh_token or refresh_token,
                    session_id=identity.session_id or session_id,
                    auth_time=auth_time,
                )
        except (httpx.HTTPError, TimeoutError):
            raise AuthError(503) from None
        except (OAuthError, JoseError, ValueError, KeyError, TypeError):
            raise AuthError(401) from None

    async def validate_logout_token(self, encoded: str) -> LogoutNotice:
        """Validate the distinct signed logout-token profile without accepting ID tokens."""
        if not encoded or len(encoded) > 65_536:
            raise AuthError(400)
        try:
            async with asyncio.timeout(10):
                metadata = await self._discovery()
                keys = await self._json(metadata.jwks_uri)
                decoded = jwt.decode(
                    encoded,
                    KeySet.import_key_set(cast(KeySetSerialization, keys)),
                    algorithms=["RS256"],
                )
                claims = decoded.claims
                JWTClaimsRegistry(
                    iss={"essential": True, "value": self.issuer},
                    aud={"essential": True, "value": self.client_id},
                    iat={"essential": True},
                    exp={"essential": True},
                    jti={"essential": True},
                ).validate(claims)
                issued_at, expires_at = self._number(claims["iat"]), self._number(claims["exp"])
                events = claims.get("events")
                if (
                    "nonce" in claims
                    or not isinstance(events, dict)
                    or not isinstance(
                        events.get("http://schemas.openid.net/event/backchannel-logout"), dict
                    )
                    or issued_at < time.time() - 900
                    or expires_at <= time.time()
                    or decoded.header.get("typ")
                    not in (None, "JWT", "logout+jwt", "application/logout+jwt")
                ):
                    raise AuthError(400)
                subject = self._identifier(claims.get("sub"))
                sid = self._identifier(claims.get("sid"))
                event = self._identifier(claims["jti"])
                if (subject is None and sid is None) or event is None:
                    raise AuthError(400)
                return LogoutNotice(
                    self.issuer,
                    self.client_id,
                    event,
                    datetime.fromtimestamp(issued_at, UTC),
                    datetime.fromtimestamp(expires_at, UTC),
                    subject,
                    sid,
                )
        except (httpx.HTTPError, TimeoutError):
            raise AuthError(503) from None
        except (OAuthError, JoseError, ValueError, KeyError, TypeError, OverflowError):
            raise AuthError(400) from None
        except AuthError as exc:
            if exc.status == 401:
                raise AuthError(400) from None
            raise

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
