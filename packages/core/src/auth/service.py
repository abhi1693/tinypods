"""Durable local authentication with explicit transactions and fail-closed policy."""

from tinypods_core.auth.contracts import AuthError, Login
from tinypods_core.auth.sessions import IdentityProvider, SessionService
from tinypods_core.db import Database
from tinypods_core.db.models import Installation
from tinypods_core.rate_limit import RateLimiter, RatePolicy

LOGIN_GLOBAL = RatePolicy("auth:login:global", 30, 60_000)


class AuthService[Proof]:
    """Own authentication transactions; never fall back when the database fails.

    The composition root selects one provider. Its proof type need not contain
    passwords. Global Valkey reservations precede verification; provider-specific
    throttles and proof checks belong to the selected adapter. Failures never
    trigger another provider. Session and authorization behavior is shared.
    """

    def __init__(
        self,
        database: Database,
        provider: IdentityProvider[Proof],
        limiter: RateLimiter | None = None,
        *,
        session_seconds: int = 43200,
    ) -> None:
        """Retain the process-owned database and fixed session lifetime."""
        self.database = database
        self.provider = provider
        self.limiter = limiter
        self.sessions = SessionService(database, session_seconds=session_seconds)

    async def _reserve(self, policy: RatePolicy, subject: str) -> None:
        """Layer authentication throttles on shared fail-closed Valkey reservations."""
        if self.limiter is None:
            raise AuthError(503)
        decision = await self.limiter.reserve(policy, subject)
        if not decision.allowed:
            raise AuthError(429, retry_after=decision.retry_after)

    async def login(
        self,
        proof: Proof,
        *,
        previous_digest: str | None = None,
        previous_refresh_digest: str | None = None,
    ) -> Login:
        """Issue a random revocable session after rate-limited provider verification.

        The installation-wide limit is 30 attempts/minute including successes.
        The injected provider owns its proof validation and any additional policy;
        generic orchestration neither reads passwords nor chooses another provider.
        """
        async with self.database.session() as session:
            installation = await session.get(Installation, 1)
            if installation is None:
                raise AuthError(503)
            installation_id = str(installation.installation_id)
        await self._reserve(LOGIN_GLOBAL, installation_id)
        user_id = await self.provider.verify(proof)
        return await self.sessions.issue(
            user_id,
            previous_digest=previous_digest,
            previous_refresh_digest=previous_refresh_digest,
        )
