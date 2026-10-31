"""Provider-independent session issuance, validation, revocation and authorization."""

import secrets
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.auth.contracts import AuthError, Login, Principal, Scope
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database
from tinypods_core.db.models import AuthSession, User
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
        source_provider: str = "local",
        external_issuer: str | None = None,
        external_subject: str | None = None,
    ) -> Login:
        """Issue a browser session for a verified identity, rotating any prior browser session.

        This is a trusted internal boundary, not a public bearer-token acceptance API.
        Future API-key adapters can supply their own credential validation without
        requiring a password; key issuance and non-browser credentials are deferred.
        """
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with self.database.transaction() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise AuthError(401)
            now = (await session.exec(select(func.clock_timestamp()))).one()
            record = AuthSession(
                source_provider=source_provider,
                external_issuer=external_issuer,
                external_subject=external_subject,
                token_digest=digest(token),
                user_id=user.id,
                csrf_digest=digest(csrf),
                expires_at=now + timedelta(seconds=self.session_seconds),
            )
            session.add(record)
            if previous_digest is not None:
                previous = await session.get(AuthSession, previous_digest)
                if previous is not None:
                    previous.revoked_at = now
                    session.add(previous)
        return Login(self._principal(user, record), record.expires_at, token, csrf)

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
                    and record.external_subject in self.settings.zitadel_allowed_subjects
                )
            else:
                enabled = False
            if not enabled:
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
            record.revoked_at = (await session.exec(select(func.clock_timestamp()))).one()
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
