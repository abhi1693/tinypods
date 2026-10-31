"""Supported redirect providers, bounded browser login transactions and explicit identity access."""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func
from sqlmodel import col, select
from tinypods_core.auth.contracts import AuthError, Login, VerifiedIdentity
from tinypods_core.auth.sessions import SessionService
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database
from tinypods_core.db.models import ExternalIdentity, Installation, LoginTransaction, User
from tinypods_core.rate_limit import RateLimiter, RatePolicy
from tinypods_core.settings import Settings

logger = logging.getLogger(__name__)


class RedirectProvider(Protocol):
    """Describe browser challenge/exchange/logout without exposing OAuth details to routes."""

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """Build a fresh authorization challenge for a server-owned transaction."""
        ...

    async def exchange(self, code: str, nonce: str, verifier: str) -> VerifiedIdentity:
        """Return a cryptographically verified identity or fail without fallback."""
        ...

    async def logout_url(self) -> str | None:
        """Return an optional provider logout target without browser-exposed tokens."""
        ...


@dataclass(frozen=True)
class LoginStart:
    """Return an authorization target and an opaque browser binding, never the verifier."""

    authorization_url: str
    browser_token: str = field(repr=False)


@dataclass(frozen=True)
class IdentityPolicy:
    """Capture a configured provider's issuer and explicit identity admission policy."""

    issuer: str | None
    enabled: bool


def identity_policy(settings: Settings, provider: str) -> IdentityPolicy:
    """Resolve a supported provider centrally; arbitrary issuers and local linking are rejected."""
    policies = {"zitadel": IdentityPolicy(settings.zitadel_issuer, settings.zitadel_enabled)}
    if provider not in policies:
        raise AuthError(404)
    return policies[provider]


class IdentityMappings:
    """Provision verified identities; reserve account linking for trusted operators."""

    def __init__(
        self, database: Database, settings: Settings, *, provider: str = "zitadel"
    ) -> None:
        """Retain deployment-owned issuer/access policy and the durable identity store."""
        self.database = database
        self.settings = settings
        self.provider = provider

    async def resolve(self, identity: VerifiedIdentity) -> UUID:
        """Map verified issuer and subject to an account, provisioning non-owners on first login."""
        policy = identity_policy(self.settings, self.provider)
        if not policy.enabled or identity.issuer != policy.issuer:
            raise AuthError(403)
        async with self.database.transaction() as session:
            installation = (
                await session.exec(select(Installation).with_for_update())
            ).one_or_none()
            if installation is None:
                raise AuthError(503)
            mapping = (
                await session.exec(
                    select(ExternalIdentity).where(
                        ExternalIdentity.issuer == identity.issuer,
                        ExternalIdentity.subject == identity.subject,
                    )
                )
            ).one_or_none()
            if mapping is not None:
                return mapping.user_id
            user = User(username="oidc_" + uuid4().hex, is_owner=False)
            session.add(user)
            await session.flush()
            session.add(
                ExternalIdentity(issuer=identity.issuer, subject=identity.subject, user_id=user.id)
            )
            return user.id

    async def link(self, subject: str, user_id: UUID) -> None:
        """Explicitly link a subject to an existing account from an operator-only CLI.

        Existing mappings cannot be reassigned. No email or role claim is consulted.
        The caller must be an authorized deployment operator; this is not a public route.
        """
        policy = identity_policy(self.settings, self.provider)
        if not policy.enabled:
            raise AuthError(403)
        async with self.database.transaction() as session:
            installation = (
                await session.exec(select(Installation).with_for_update())
            ).one_or_none()
            if installation is None:
                raise AuthError(503)
            if await session.get(User, user_id) is None:
                raise AuthError(404)
            mapping = (
                await session.exec(
                    select(ExternalIdentity).where(
                        ExternalIdentity.issuer == policy.issuer,
                        ExternalIdentity.subject == subject,
                    )
                )
            ).one_or_none()
            if mapping is not None:
                raise AuthError(409)
            session.add(
                ExternalIdentity(issuer=cast(str, policy.issuer), subject=subject, user_id=user_id)
            )


class BrowserLogin:
    """Own single-use browser transactions independently of the selected supported adapter."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        provider: RedirectProvider,
        limiter: RateLimiter | None,
    ) -> None:
        """Bind one selected adapter and policy; never activate another on failure."""
        self.database = database
        self.settings = settings
        self.provider = provider
        self.limiter = limiter
        self.identities = IdentityMappings(database, settings)
        self.sessions = SessionService(database, settings=settings)

    def configuration_digest(self) -> str:
        """Bind transactions to exact configuration, including access and client secrets."""
        secret = self.settings.zitadel_client_secret
        return digest(
            json.dumps(
                [
                    self.settings.zitadel_enabled,
                    self.settings.zitadel_issuer,
                    self.settings.zitadel_client_id,
                    self.settings.zitadel_organization_id,
                    self.settings.zitadel_identity_provider_id,
                    self.settings.zitadel_redirect_uri,
                    self.settings.zitadel_token_auth_method,
                    secret.get_secret_value() if secret is not None else None,
                ]
            )
        )

    async def start(
        self,
        return_to: str = "/",
        previous_token: str | None = None,
        *,
        browser_token: str | None = None,
    ) -> LoginStart:
        """Reserve a start, persist five-minute secrets and return a fresh challenge."""
        if not self.settings.zitadel_enabled:
            raise AuthError(404)
        if len(return_to) > 256 or not re.fullmatch(
            r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*", return_to
        ):
            raise AuthError(400)
        if self.limiter is None:
            raise AuthError(503)
        async with self.database.session() as session:
            installation = await session.get(Installation, 1)
            if installation is None:
                raise AuthError(503)
            subject = str(installation.installation_id)
        decision = await self.limiter.reserve(RatePolicy("auth:oidc:start", 30, 60_000), subject)
        if not decision.allowed:
            raise AuthError(429, retry_after=decision.retry_after)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        browser = (
            browser_token
            if browser_token and len(browser_token) <= 128
            else secrets.token_urlsafe(32)
        )
        url = await self.provider.authorization_url(state, nonce, verifier)
        async with self.database.transaction() as session:
            now = (await session.exec(select(func.clock_timestamp()))).one()
            await session.exec(delete(LoginTransaction).where(LoginTransaction.expires_at <= now))
            session.add(
                LoginTransaction(
                    state_digest=digest(state),
                    browser_digest=digest(browser),
                    nonce=nonce,
                    verifier=verifier,
                    configuration_digest=self.configuration_digest(),
                    return_to=return_to,
                    expires_at=now + timedelta(minutes=5),
                    previous_session_digest=digest(previous_token)
                    if previous_token is not None
                    else None,
                )
            )
        return LoginStart(url, browser)

    async def finish(
        self,
        state: str,
        browser_token: str,
        code: str | None,
        *,
        error: str | None = None,
        response_issuer: str | None = None,
    ) -> tuple[Login, str]:
        """Consume atomically before exchange, then admit identity and rotate a core session."""
        if not self.settings.zitadel_enabled:
            raise AuthError(404)
        if not state or not browser_token or len(state) > 128 or len(browser_token) > 128:
            logger.warning("Login callback rejected: missing or invalid browser correlation")
            raise AuthError(401)
        async with self.database.transaction() as session:
            statement = (
                delete(LoginTransaction)
                .where(
                    col(LoginTransaction.state_digest) == digest(state),
                    col(LoginTransaction.browser_digest) == digest(browser_token),
                )
                .returning(LoginTransaction)
            )
            result = await session.exec(statement)
            transaction = cast(LoginTransaction | None, result.scalar_one_or_none())
            now = (await session.exec(select(func.clock_timestamp()))).one()
        if transaction is None:
            logger.warning("Login callback rejected: transaction absent or browser mismatch")
            raise AuthError(401)
        if (
            transaction.expires_at <= now
            or transaction.configuration_digest != self.configuration_digest()
            or error is not None
            or not code
            or len(code) > 4096
            or (response_issuer is not None and response_issuer != self.settings.zitadel_issuer)
        ):
            raise AuthError(401)
        logger.debug("Login callback correlation accepted; exchanging authorization code")
        identity = await self.provider.exchange(code, transaction.nonce, transaction.verifier)
        user_id = await self.identities.resolve(identity)
        login = await self.sessions.issue(
            user_id,
            previous_digest=transaction.previous_session_digest,
            source_provider="zitadel",
            external_issuer=identity.issuer,
            external_subject=identity.subject,
        )
        return login, transaction.return_to
