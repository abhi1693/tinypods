"""Provider-independent session issuance, validation, revocation and authorization."""

import secrets
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.auth.contracts import AuthError, Login, Principal, Scope
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database
from tinypods_core.db.models import AuthSession, ProviderGrant, RefreshCredential, User
from tinypods_core.settings import Settings


class IdentityProvider[Proof](Protocol):
    """Verify provider-specific proof and return a core account ID without credential coupling."""

    async def verify(self, proof: Proof) -> UUID:
        """Return an authenticated core identity or raise; never silently fall back."""
        ...


class SessionService:
    """Own durable sessions and policy for identities from any trusted core adapter."""

    def __init__(
        self,
        database: Database,
        *,
        session_seconds: int = 43200,
        settings: Settings | None = None,
    ) -> None:
        """Retain the process database and absolute session lifetime."""
        self.database = database
        self.settings = settings
        self.session_seconds = session_seconds

    async def login[Proof](
        self,
        provider: IdentityProvider[Proof],
        proof: Proof,
        *,
        previous_digest: str | None = None,
    ) -> Login:
        """Issue a session only after the selected provider authenticates its proof.

        Provider adapters are trusted application code, never selected by public
        request fields. Unknown core account IDs are rejected. This boundary adds
        no external discovery, provider fallback or account provisioning behavior.
        """
        user_id = await provider.verify(proof)
        return await self.issue(user_id, previous_digest=previous_digest)

    async def issue(
        self,
        user_id: UUID,
        *,
        previous_digest: str | None = None,
        previous_refresh_digest: str | None = None,
        source_provider: str = "local",
        external_issuer: str | None = None,
        external_subject: str | None = None,
        external_client_id: str | None = None,
        external_session_id: str | None = None,
        authenticated_at: datetime | None = None,
    ) -> Login:
        """Issue a session after trusted provider verification and revoke previous credentials."""
        async with self.database.transaction() as session:
            return await self.issue_in_transaction(
                session,
                user_id,
                previous_digest=previous_digest,
                previous_refresh_digest=previous_refresh_digest,
                source_provider=source_provider,
                external_issuer=external_issuer,
                external_subject=external_subject,
                external_client_id=external_client_id,
                external_session_id=external_session_id,
                authenticated_at=authenticated_at,
            )

    async def issue_in_transaction(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        previous_digest: str | None = None,
        previous_refresh_digest: str | None = None,
        source_provider: str = "local",
        external_issuer: str | None = None,
        external_subject: str | None = None,
        external_client_id: str | None = None,
        external_session_id: str | None = None,
        authenticated_at: datetime | None = None,
        grant_id: UUID | None = None,
        refresh_expires_at: datetime | None = None,
    ) -> Login:
        """Atomically issue cookies and optional refresh credentials in the caller's transaction.

        The caller owns commit/rollback. Browser refresh values are random and only
        their digests are retained. Provider refresh material belongs to the grant.
        """
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        user = await session.get(User, user_id)
        if user is None:
            raise AuthError(401)
        now = (await session.exec(select(func.clock_timestamp()))).one()
        expires = now + timedelta(seconds=self.session_seconds)
        if refresh_expires_at is not None:
            expires = min(expires, refresh_expires_at)
        record = AuthSession(
            source_provider=source_provider,
            external_issuer=external_issuer,
            external_subject=external_subject,
            external_client_id=external_client_id,
            external_session_id=external_session_id,
            provider_authenticated_at=authenticated_at,
            external_grant_id=grant_id,
            token_digest=digest(token),
            user_id=user.id,
            csrf_digest=digest(csrf),
            expires_at=expires,
        )
        if previous_digest is not None:
            previous = await session.get(AuthSession, previous_digest)
            if previous is not None:
                if (
                    previous.external_grant_id is not None
                    and previous.external_grant_id != grant_id
                ):
                    await self.revoke_grant(session, previous.external_grant_id, now)
                previous.revoked_at = now
                session.add(previous)
        if previous_refresh_digest is not None:
            previous_refresh = await session.get(RefreshCredential, previous_refresh_digest)
            if previous_refresh is not None and previous_refresh.grant_id != grant_id:
                await self.revoke_grant(session, previous_refresh.grant_id, now)
        session.add(record)
        refresh: str | None = None
        if grant_id is not None:
            refresh = secrets.token_urlsafe(32)
            session.add(
                RefreshCredential(
                    token_digest=digest(refresh),
                    csrf_digest=digest(csrf),
                    grant_id=grant_id,
                )
            )
        return Login(
            self._principal(user, record),
            record.expires_at,
            token,
            csrf,
            refresh,
            refresh_expires_at,
        )

    @staticmethod
    async def revoke_grant(session: AsyncSession, grant_id: UUID, now: datetime) -> None:
        """Revoke a refresh family and all descendants under its database row lock."""
        grant = (
            await session.exec(
                select(ProviderGrant).where(ProviderGrant.id == grant_id).with_for_update()
            )
        ).one_or_none()
        if grant is not None:
            grant.status, grant.encrypted_state = "revoked", None
            session.add(grant)
            await session.exec(
                update(AuthSession)
                .where(col(AuthSession.external_grant_id) == grant_id)
                .values(revoked_at=now)
            )

    @staticmethod
    async def refresh_context(
        session: AsyncSession, refresh_token: str, csrf_token: str
    ) -> tuple[ProviderGrant, RefreshCredential]:
        """Lock a known refresh family and verify its browser-bound CSRF proof."""
        if not refresh_token or len(refresh_token) > 128:
            raise AuthError(401)
        credential = await session.get(RefreshCredential, digest(refresh_token))
        if credential is None:
            raise AuthError(401)
        grant = (
            await session.exec(
                select(ProviderGrant)
                .where(ProviderGrant.id == credential.grant_id)
                .with_for_update()
            )
        ).one_or_none()
        if grant is None:
            raise AuthError(401)
        if not secrets.compare_digest(credential.csrf_digest, digest(csrf_token)):
            raise AuthError(403)
        return grant, credential

    async def logout_browser(self, token: str, csrf_token: str, refresh_token: str | None) -> None:
        """Sign out a live session, or its refresh family after the access cookie expires."""
        try:
            principal = await self.authenticate(token, csrf_token=csrf_token)
        except AuthError as exc:
            if exc.status != 401 or refresh_token is None:
                raise
            async with self.database.transaction() as session:
                grant, _ = await self.refresh_context(session, refresh_token, csrf_token)
                now = (await session.exec(select(func.clock_timestamp()))).one()
                await self.revoke_grant(session, grant.id, now)
        else:
            await self.logout(principal)

    @staticmethod
    def _principal(user: User, record: AuthSession) -> Principal:
        """Copy persistence identity into a normalized principal without password data."""
        return Principal(user.id, user.username, user.is_owner, record.token_digest)

    async def _identity(self, session: AsyncSession, token_digest: str) -> tuple[User, AuthSession]:
        """Load only a live, unrevoked session with a surviving core account."""
        result = (
            await session.exec(
                select(User, AuthSession)
                .join(AuthSession, col(AuthSession.user_id) == col(User.id))
                .where(
                    AuthSession.token_digest == token_digest,
                    AuthSession.expires_at > func.clock_timestamp(),
                    col(AuthSession.revoked_at).is_(None),
                )
            )
        ).one_or_none()
        if result is None:
            raise AuthError(401)
        user, record = result[0], result[1]
        if self.settings is not None:
            if record.source_provider == "local":
                enabled = self.settings.auth_local_enabled
            elif record.source_provider == "zitadel":
                enabled = (
                    self.settings.zitadel_enabled
                    and record.external_issuer == self.settings.zitadel_issuer
                )
            else:
                enabled = False
            if not enabled:
                raise AuthError(401)
        if record.external_grant_id is not None:
            grant = await session.get(ProviderGrant, record.external_grant_id)
            now = (await session.exec(select(func.clock_timestamp()))).one()
            if grant is None or grant.status != "active" or grant.expires_at <= now:
                raise AuthError(401)
        return user, record

    async def authenticate(self, token: str, *, csrf_token: str | None = None) -> Principal:
        """Validate session and, for mutations, its independently generated CSRF token."""
        async with self.database.session() as session:
            user, record = await self._identity(session, digest(token))
            if csrf_token is not None and not secrets.compare_digest(
                record.csrf_digest, digest(csrf_token)
            ):
                raise AuthError(403)
            return self._principal(user, record)

    async def logout(self, principal: Principal) -> None:
        """Revalidate and durably revoke the caller's session before reporting success."""
        async with self.database.transaction() as session:
            user, record = await self._identity(session, principal.credential_id)
            if self._principal(user, record) != principal:
                raise AuthError(403)
            now = (await session.exec(select(func.clock_timestamp()))).one()
            if record.external_grant_id is not None:
                await self.revoke_grant(session, record.external_grant_id, now)
            record.revoked_at = now
            session.add(record)

    async def validate_principal(self, principal: Principal) -> Principal:
        """Reject unsupported credential kinds and revalidate session-backed identity context."""
        if principal.credential_kind != "session":
            raise AuthError(401)
        async with self.database.session() as session:
            user, record = await self._identity(session, principal.credential_id)
            current = self._principal(user, record)
            if current != principal:
                raise AuthError(403)
            return current

    async def require_access(
        self, principal: Principal | None, scope: Scope, *, owner_id: UUID | None = None
    ) -> Principal:
        """Delegate resource policy to the credential-neutral authorization boundary."""
        from tinypods_core.auth.authorization import AuthorizationService

        return await AuthorizationService(self).require_access(principal, scope, owner_id=owner_id)
