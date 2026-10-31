"""Local password adapter; identity and session policy do not require this credential type."""

from uuid import UUID

from sqlmodel import select
from tinypods_core.auth.contracts import AuthError, Credentials
from tinypods_core.auth.providers.local.passwords import hash_password, verify_password
from tinypods_core.db import Database
from tinypods_core.db.models import Installation, LocalCredential, User
from tinypods_core.rate_limit import RateLimiter, RatePolicy

LOGIN_ACCOUNT = RatePolicy("auth:login:account", 5, 60_000)


class LocalPasswordProvider:
    """Verify local credentials without issuing sessions or granting authorization."""

    def __init__(self, database: Database, limiter: RateLimiter | None = None) -> None:
        """Retain the application-owned database."""
        self.database = database
        self.limiter = limiter

    async def bootstrap(self, credentials: Credentials) -> UUID:
        """Create the sole installation owner, serializing races on the installation row.

        Raises:
            AuthError: Bootstrap already completed (409) or installation is absent (503).
        """
        encoded = await hash_password(credentials.password.get_secret_value())
        async with self.database.transaction() as session:
            installation = (
                await session.exec(select(Installation).with_for_update())
            ).one_or_none()
            if installation is None:
                raise AuthError(503)
            if installation.owner_id is not None:
                raise AuthError(409)
            user = User(username=credentials.username, is_owner=True)
            session.add(user)
            await session.flush()
            session.add(LocalCredential(user_id=user.id, password_hash=encoded))
            installation.owner_id = user.id
            session.add(installation)
            return user.id

    async def verify(self, proof: Credentials) -> UUID:
        """Reject absent credentials, external-only identities and bad passwords uniformly."""
        async with self.database.session() as session:
            user = (
                await session.exec(select(User).where(User.username == proof.username))
            ).one_or_none()
            credential = await session.get(LocalCredential, user.id) if user else None
        if self.limiter is None:
            raise AuthError(503)
        if user is not None:
            decision = await self.limiter.reserve(LOGIN_ACCOUNT, str(user.id))
            if not decision.allowed:
                raise AuthError(429, retry_after=decision.retry_after)
        valid = await verify_password(
            credential.password_hash if credential else None, proof.password.get_secret_value()
        )
        if not valid or user is None:
            raise AuthError(401)
        return user.id
