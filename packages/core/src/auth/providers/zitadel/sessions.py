"""Encrypted refresh families and verified provider logout, with durable race fencing."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio
from sqlalchemy import and_, delete, false, func, or_, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.auth.contracts import AuthError, Login, LogoutNotice, VerifiedIdentity
from tinypods_core.auth.providers.zitadel.encryption import RefreshCipher, RefreshState
from tinypods_core.auth.providers.zitadel.oidc import ZitadelOIDC
from tinypods_core.auth.sessions import SessionService
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database
from tinypods_core.db.models import (
    AuthSession,
    Installation,
    ProviderGrant,
    ProviderLogoutEvent,
)
from tinypods_core.rate_limit import RateLimiter, RatePolicy
from tinypods_core.settings import Settings


class ZitadelSessions:
    """Rotate browser credentials and encrypted provider state without replay or resurrection.

    Refresh claims commit before upstream I/O. An ambiguous failure permanently
    revokes that family rather than retrying a possibly consumed provider token.
    Logout can revoke a claimed family while I/O is in progress; the completion
    transaction checks its generation and state before issuing any credentials.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        provider: ZitadelOIDC,
        sessions: SessionService,
        configuration_digest: Callable[[], str],
        limiter: RateLimiter | None,
    ) -> None:
        """Retain application-owned resources without importing or contacting other providers."""
        self.database, self.settings, self.provider = database, settings, provider
        self.sessions, self.configuration_digest, self.limiter = (
            sessions,
            configuration_digest,
            limiter,
        )

    async def _fence_login(
        self,
        session: AsyncSession,
        identity: VerifiedIdentity,
        authenticated: datetime,
        now: datetime,
    ) -> None:
        """Serialize new logins with logout and reject authentication predating a verified event."""
        installation = (await session.exec(select(Installation).with_for_update())).one_or_none()
        if installation is None:
            raise AuthError(503)
        same_session = (
            and_(
                col(ProviderLogoutEvent.session_id) == identity.session_id,
                or_(
                    col(ProviderLogoutEvent.subject).is_(None),
                    col(ProviderLogoutEvent.subject) == identity.subject,
                ),
            )
            if identity.session_id is not None
            else false()
        )
        prior_subject = and_(
            col(ProviderLogoutEvent.session_id).is_(None),
            col(ProviderLogoutEvent.subject) == identity.subject,
            col(ProviderLogoutEvent.issued_at) >= authenticated,
        )
        event = (
            await session.exec(
                select(ProviderLogoutEvent)
                .where(
                    ProviderLogoutEvent.issuer == identity.issuer,
                    ProviderLogoutEvent.client_id == self.settings.zitadel_client_id,
                    ProviderLogoutEvent.expires_at > now,
                    or_(same_session, prior_subject),
                )
                .limit(1)
            )
        ).one_or_none()
        if event is not None:
            raise AuthError(401)

    async def issue(
        self,
        identity: VerifiedIdentity,
        user_id: UUID,
        *,
        previous_digest: str | None,
        previous_refresh_digest: str | None,
    ) -> Login:
        """Commit provider binding, encrypted refresh state and browser credentials atomically."""
        async with self.database.transaction() as session:
            now = (await session.exec(select(func.clock_timestamp()))).one()
            authenticated = datetime.fromtimestamp(
                identity.auth_time
                if identity.auth_time is not None
                else (identity.issued_at if identity.issued_at is not None else now.timestamp()),
                UTC,
            )
            await self._fence_login(session, identity, authenticated, now)
            grant_id: UUID | None = None
            refresh_expires: datetime | None = None
            if self.settings.zitadel_refresh_enabled and identity.refresh_token is not None:
                if identity.nonce is None:
                    raise AuthError(401)
                refresh_expires = now + timedelta(
                    seconds=self.settings.zitadel_refresh_max_age_seconds
                )
                grant = ProviderGrant(
                    user_id=user_id,
                    issuer=identity.issuer,
                    client_id=self.settings.zitadel_client_id or "",
                    subject=identity.subject,
                    session_id=identity.session_id,
                    configuration_digest=self.configuration_digest(),
                    authenticated_at=authenticated,
                    expires_at=refresh_expires,
                )
                grant.encrypted_state = RefreshCipher(self.settings).encrypt(
                    RefreshState(
                        grant_id=grant.id,
                        refresh_token=identity.refresh_token,
                        nonce=identity.nonce,
                        auth_time=identity.auth_time,
                    )
                )
                session.add(grant)
                await session.flush()
                grant_id = grant.id
            return await self.sessions.issue_in_transaction(
                session,
                user_id,
                previous_digest=previous_digest,
                previous_refresh_digest=previous_refresh_digest,
                source_provider="zitadel",
                external_issuer=identity.issuer,
                external_subject=identity.subject,
                external_client_id=self.settings.zitadel_client_id,
                external_session_id=identity.session_id,
                authenticated_at=authenticated,
                grant_id=grant_id,
                refresh_expires_at=refresh_expires,
            )

    async def _claim(
        self, refresh_token: str, csrf_token: str
    ) -> tuple[UUID, int, RefreshState, str, str | None]:
        """Consume one browser credential durably before using its provider refresh token."""
        failure: AuthError | None = None
        state: RefreshState | None = None
        async with self.database.transaction() as session:
            grant, credential = await self.sessions.refresh_context(
                session, refresh_token, csrf_token
            )
            now = (await session.exec(select(func.clock_timestamp()))).one()
            if (
                not self.settings.zitadel_refresh_enabled
                or not self.settings.zitadel_enabled
                or grant.expires_at <= now
                or grant.status == "revoked"
                or grant.issuer != self.settings.zitadel_issuer
                or grant.client_id != self.settings.zitadel_client_id
                or grant.configuration_digest != self.configuration_digest()
            ):
                await self.sessions.revoke_grant(session, grant.id, now)
                failure = AuthError(401)
            elif grant.status == "refreshing":
                if credential.consumed_at is None or credential.consumed_at <= now - timedelta(
                    seconds=30
                ):
                    await self.sessions.revoke_grant(session, grant.id, now)
                    failure = AuthError(401)
                else:
                    failure = AuthError(409)
            elif credential.consumed_at is not None or grant.encrypted_state is None:
                await self.sessions.revoke_grant(session, grant.id, now)
                failure = AuthError(401)
            else:
                try:
                    state = RefreshCipher(self.settings).decrypt(grant.encrypted_state, grant.id)
                except AuthError as exc:
                    await self.sessions.revoke_grant(session, grant.id, now)
                    failure = exc
                else:
                    if self.limiter is None:
                        raise AuthError(503)
                    decision = await self.limiter.reserve(
                        RatePolicy("auth:oidc:refresh", 10, 60_000), str(grant.id)
                    )
                    if not decision.allowed:
                        raise AuthError(429, retry_after=decision.retry_after)
                    credential.consumed_at = now
                    grant.status = "refreshing"
                    grant.generation += 1
                    session.add(credential)
                    session.add(grant)
            grant_id, generation, subject, sid = (
                grant.id,
                grant.generation,
                grant.subject,
                grant.session_id,
            )
        if failure is not None:
            raise failure
        assert state is not None
        return grant_id, generation, state, subject, sid

    async def _revoke_after_failure(self, grant_id: UUID) -> None:
        """Keep cancellation from leaving a reusable credential after ambiguous upstream I/O."""
        with anyio.CancelScope(shield=True):
            async with self.database.transaction() as session:
                now = (await session.exec(select(func.clock_timestamp()))).one()
                await self.sessions.revoke_grant(session, grant_id, now)

    async def refresh(self, refresh_token: str, csrf_token: str) -> Login:
        """Rotate access, refresh and CSRF cookies after one serialized provider refresh."""
        grant_id, generation, state, subject, sid = await self._claim(refresh_token, csrf_token)
        try:
            identity = await self.provider.refresh(
                state.refresh_token,
                state.nonce,
                subject=subject,
                session_id=sid,
                auth_time=state.auth_time,
            )
            async with self.database.transaction() as session:
                grant = (
                    await session.exec(
                        select(ProviderGrant).where(ProviderGrant.id == grant_id).with_for_update()
                    )
                ).one_or_none()
                if grant is None or grant.status != "refreshing" or grant.generation != generation:
                    raise AuthError(401)
                now = (await session.exec(select(func.clock_timestamp()))).one()
                if (
                    grant.expires_at <= now
                    or identity.issuer != grant.issuer
                    or identity.subject != grant.subject
                    or identity.refresh_token is None
                ):
                    raise AuthError(401)
                grant.session_id = identity.session_id
                grant.encrypted_state = RefreshCipher(self.settings).encrypt(
                    RefreshState(
                        grant_id=grant.id,
                        refresh_token=identity.refresh_token,
                        nonce=state.nonce,
                        auth_time=state.auth_time,
                    )
                )
                grant.status = "active"
                session.add(grant)
                await session.exec(
                    update(AuthSession)
                    .where(col(AuthSession.external_grant_id) == grant.id)
                    .values(revoked_at=now)
                )
                login = await self.sessions.issue_in_transaction(
                    session,
                    grant.user_id,
                    previous_refresh_digest=digest(refresh_token),
                    source_provider="zitadel",
                    external_issuer=grant.issuer,
                    external_subject=grant.subject,
                    external_client_id=grant.client_id,
                    external_session_id=grant.session_id,
                    authenticated_at=grant.authenticated_at,
                    grant_id=grant.id,
                    refresh_expires_at=grant.expires_at,
                )
            return login
        except BaseException:
            await self._revoke_after_failure(grant_id)
            raise

    async def backchannel_logout(self, encoded: str) -> None:
        """Validate and deduplicate a logout, then revoke only matching provider sessions."""
        notice = await self.provider.validate_logout_token(encoded)
        await self.apply_logout(notice)

    async def apply_logout(self, notice: LogoutNotice) -> None:
        """Fence in-flight logins and refresh completions in the same revocation transaction."""
        if (
            notice.issuer != self.settings.zitadel_issuer
            or notice.client_id != self.settings.zitadel_client_id
            or (notice.subject is None and notice.session_id is None)
        ):
            raise AuthError(400)
        async with self.database.transaction() as session:
            installation = (
                await session.exec(select(Installation).with_for_update())
            ).one_or_none()
            if installation is None:
                raise AuthError(503)
            now = (await session.exec(select(func.clock_timestamp()))).one()
            await session.exec(
                delete(ProviderLogoutEvent).where(ProviderLogoutEvent.expires_at <= now)
            )
            event_id = digest(json.dumps([notice.issuer, notice.client_id, notice.event_id]))
            if await session.get(ProviderLogoutEvent, event_id) is not None:
                return
            session.add(
                ProviderLogoutEvent(
                    event_digest=event_id,
                    issuer=notice.issuer,
                    client_id=notice.client_id,
                    subject=notice.subject,
                    session_id=notice.session_id,
                    issued_at=notice.issued_at,
                    expires_at=max(notice.expires_at, now + timedelta(minutes=5)),
                )
            )
            grants = select(ProviderGrant).where(
                ProviderGrant.issuer == notice.issuer,
                ProviderGrant.client_id == notice.client_id,
            )
            sessions = update(AuthSession).where(
                col(AuthSession.source_provider) == "zitadel",
                col(AuthSession.external_issuer) == notice.issuer,
                col(AuthSession.external_client_id) == notice.client_id,
            )
            if notice.session_id is not None:
                grants = grants.where(ProviderGrant.session_id == notice.session_id)
                sessions = sessions.where(col(AuthSession.external_session_id) == notice.session_id)
            else:
                grants = grants.where(ProviderGrant.authenticated_at <= notice.issued_at)
                sessions = sessions.where(
                    or_(
                        col(AuthSession.provider_authenticated_at).is_(None),
                        col(AuthSession.provider_authenticated_at) <= notice.issued_at,
                    )
                )
            if notice.subject is not None:
                grants = grants.where(ProviderGrant.subject == notice.subject)
                sessions = sessions.where(col(AuthSession.external_subject) == notice.subject)
            for grant in (await session.exec(grants.order_by(col(ProviderGrant.id)))).all():
                await self.sessions.revoke_grant(session, grant.id, now)
            await session.exec(sessions.values(revoked_at=now))
