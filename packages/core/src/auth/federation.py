"""Supported redirect providers, bounded browser login transactions and explicit identity access."""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func
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

    async def authorization_url(
        self, state: str, nonce: str, verifier: str, *, identity_provider_id: str | None = None
    ) -> str:
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
        """Map verified identity; only the first account of a new instance becomes its owner."""
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
            # The singleton lock serializes local and external bootstrap together.
            # The persisted first-owner marker prevents later sign-ins from claiming it.
            first_user = installation.owner_id is None
            user = User(username="oidc_" + uuid4().hex, is_owner=first_user)
            session.add(user)
            await session.flush()
            if first_user:
                installation.owner_id = user.id
                session.add(installation)
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


class ProviderSessionLifecycle(Protocol):
    """Commit optional provider-specific state alongside shared browser sessions."""

    async def issue(
        self,
        identity: VerifiedIdentity,
        user_id: UUID,
        *,
        previous_digest: str | None,
        previous_refresh_digest: str | None,
    ) -> Login:
        """Issue a normalized session after trusted identity verification."""
        ...


def validate_return_destination(return_to: str, allowed_origins: list[str]) -> None:
    """Reject external redirects except exact configured browser origins.

    Keep paths simple and query-free until the frontend implements deep-link capture.
    Validate before persistence so callbacks never accept a new redirect parameter.
    """
    try:
        destination = urlsplit(return_to)
    except ValueError:
        raise AuthError(400) from None
    path = return_to
    if destination.scheme or destination.netloc:
        origin = f"{destination.scheme}://{destination.netloc}"
        if origin not in allowed_origins:
            raise AuthError(400)
        path = destination.path
    if (
        len(return_to) > 256
        or destination.query
        or destination.fragment
        or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*", path)
    ):
        raise AuthError(400)


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
        self.lifecycle: ProviderSessionLifecycle | None = None

    def configuration_digest(self) -> str:
        """Bind transactions to exact configuration, including access and client secrets."""
        secret = self.settings.zitadel_client_secret
        return digest(
            json.dumps(
                [
                    self.settings.zitadel_enabled,
                    self.settings.zitadel_refresh_enabled,
                    self.settings.zitadel_issuer,
                    self.settings.zitadel_client_id,
                    self.settings.zitadel_organization_id,
                    self.settings.zitadel_organization_domain,
                    [provider.id for provider in self.settings.zitadel_identity_providers],
                    self.settings.browser_origins,
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
        identity_provider_id: str | None = None,
        previous_refresh_token: str | None = None,
    ) -> LoginStart:
        """Rate-limit initiation and persist a five-minute browser-bound challenge.

        State correlates the callback, nonce binds its ID token, and the PKCE verifier
        binds the authorization-code exchange. Their raw values never enter logs.
        """
        if not self.settings.zitadel_enabled:
            raise AuthError(404)
        if identity_provider_id is not None and identity_provider_id not in {
            provider.id for provider in self.settings.zitadel_identity_providers
        }:
            raise AuthError(400)
        # Only explicitly configured provider choices and return origins enter state.
        validate_return_destination(return_to, self.settings.browser_origins)
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
        # Retain a bounded correlation cookie for parallel tabs, but mint fresh
        # state/nonce/PKCE material for every attempt.
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        browser = (
            browser_token
            if browser_token and len(browser_token) <= 128
            else secrets.token_urlsafe(32)
        )
        url = await self.provider.authorization_url(
            state, nonce, verifier, identity_provider_id=identity_provider_id
        )
        async with self.database.transaction() as session:
            now = (await session.exec(select(func.clock_timestamp()))).one()
            expired = await session.exec(
                select(LoginTransaction).where(LoginTransaction.expires_at <= now).with_for_update()
            )
            for transaction in expired.all():
                await session.delete(transaction)
            session.add(
                LoginTransaction(
                    state_digest=digest(state),
                    browser_digest=digest(browser),
                    nonce=nonce,
                    verifier=verifier,
                    configuration_digest=self.configuration_digest(),
                    return_to=return_to,
                    expires_at=now + timedelta(minutes=5),
                    previous_refresh_digest=digest(previous_refresh_token)
                    if previous_refresh_token is not None
                    else None,
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
        """Consume a correlated attempt once, verify the provider, then issue a session.

        Callback/provider errors deliberately consume the attempt; restarting sign-in
        creates fresh correlation material rather than replaying an uncertain exchange.
        """
        if not self.settings.zitadel_enabled:
            raise AuthError(404)
        if not state or not browser_token or len(state) > 128 or len(browser_token) > 128:
            logger.warning("Login callback rejected: missing or invalid browser correlation")
            raise AuthError(401)
        async with self.database.transaction() as session:
            # Match both state and the HttpOnly browser cookie under a row lock.
            # A wrong browser cannot consume another browser's attempt.
            transaction = (
                await session.exec(
                    select(LoginTransaction)
                    .where(
                        col(LoginTransaction.state_digest) == digest(state),
                        col(LoginTransaction.browser_digest) == digest(browser_token),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if transaction is not None:
                await session.delete(transaction)
            now = (await session.exec(select(func.clock_timestamp()))).one()
        # Commit consumption before any network call. Replays and concurrent
        # callbacks cannot exchange the same attempt, including after failures.
        if transaction is None:
            logger.warning("Login callback rejected: transaction absent or browser mismatch")
            raise AuthError(401)
        if (
            transaction.expires_at <= now
            or transaction.configuration_digest != self.configuration_digest()
        ):
            raise AuthError(401)
        if error is not None or not code or len(code) > 4096:
            raise AuthError(401)
        if response_issuer is not None and response_issuer != self.settings.zitadel_issuer:
            raise AuthError(401)
        # The adapter verifies issuer/signature/audience and binds nonce/PKCE.
        # Admission then resolves identity; token claims do not grant ownership.
        logger.debug("Login callback correlation accepted; exchanging authorization code")
        identity = await self.provider.exchange(code, transaction.nonce, transaction.verifier)
        user_id = await self.identities.resolve(identity)
        # The optional provider lifecycle owns refresh state; both paths rotate
        # previous browser credentials only while issuing the verified new session.
        if self.lifecycle is not None:
            login = await self.lifecycle.issue(
                identity,
                user_id,
                previous_digest=transaction.previous_session_digest,
                previous_refresh_digest=transaction.previous_refresh_digest,
            )
        else:
            login = await self.sessions.issue(
                user_id,
                previous_digest=transaction.previous_session_digest,
                previous_refresh_digest=transaction.previous_refresh_digest,
                source_provider="zitadel",
                external_issuer=identity.issuer,
                external_subject=identity.subject,
                external_client_id=self.settings.zitadel_client_id,
                external_session_id=identity.session_id,
            )
        return login, transaction.return_to
