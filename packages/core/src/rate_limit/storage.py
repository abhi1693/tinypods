"""Reusable atomic fixed-window rate limits on a shared Valkey service."""

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.maint_notifications import MaintNotificationsConfig
from tinypods_core.settings import Settings

# A reservation and its expiry are one atomic operation, including the first hit.
# Denied requests neither increase the counter nor extend the original window.
RESERVE = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local ttl = redis.call('PTTL', KEYS[1])
if count > 0 and ttl < 0 then return redis.error_reply('Invalid rate-limit expiry') end
if count >= tonumber(ARGV[1]) then return {0, ttl} end
if count == 0 then
    redis.call('SET', KEYS[1], 1, 'PX', ARGV[2])
    return {1, tonumber(ARGV[2])}
end
redis.call('INCR', KEYS[1])
return {1, ttl}
"""


class RateLimitUnavailable(Exception):
    """Report a bounded storage failure without leaking connection or server details."""


@dataclass(frozen=True)
class RatePolicy:
    """Name a reusable request budget and its fixed window in milliseconds."""

    name: str
    limit: int
    window_ms: int

    def __post_init__(self) -> None:
        """Reject invalid policy names and unbounded or nonpositive budgets."""
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{1,80}", self.name):
            raise ValueError("Invalid rate policy name")
        if not 1 <= self.limit <= 1_000_000 or not 1 <= self.window_ms <= 86_400_000:
            raise ValueError("Invalid rate policy budget")


@dataclass(frozen=True)
class RateDecision:
    """Return a reservation outcome and the remaining wait before retrying."""

    allowed: bool
    retry_after: int


class RateLimiter:
    """Own a bounded native async Valkey pool; callers decide how to handle denial.

    One key per active policy/subject expires automatically. Hashing hides subjects
    from keys; callers should bound subject cardinality for their use case. This
    mechanism does not use response-cache fallbacks or process-local counters.
    Eviction or Valkey data loss resets budgets; use a non-evicting service with
    persistence when that matters. Storage failures never produce an allowance.
    """

    def __init__(self, settings: Settings) -> None:
        """Create a lazily connected pool independent of the optional public cache."""
        configured = settings.rate_limit_valkey_url or settings.valkey_url
        if configured is None:
            raise ValueError("A Valkey URL is required for rate limiting")
        options: dict[str, Any] = {
            "socket_timeout": 0.2,
            "socket_connect_timeout": 0.2,
            "retry": Retry(NoBackoff(), 0),
            "decode_responses": False,
        }
        self.redis = Redis.from_url(
            configured.get_secret_value(),
            max_connections=16,
            maint_notifications_config=MaintNotificationsConfig(enabled=False),
            **options,
        )
        # URL query parameters cannot silently enlarge deadlines, retries or pool size.
        self.redis.connection_pool.connection_kwargs.update(options)
        self.redis.connection_pool.max_connections = 16
        self.namespace = settings.rate_limit_namespace

    def key(self, policy: RatePolicy, subject: str) -> str:
        """Build an opaque key isolated by namespace, policy configuration and subject."""
        identity = f"{policy.name}:{policy.limit}:{policy.window_ms}:{subject}"
        return f"{self.namespace}:{hashlib.sha256(identity.encode()).hexdigest()}"

    async def reserve(self, policy: RatePolicy, subject: str) -> RateDecision:
        """Reserve one request atomically; transport/server failures raise unavailable.

        Cancellation and programming errors propagate. No automatic retry can
        duplicate an ambiguously completed reservation. A lost reply may consume
        budget, but it never permits an unchecked request.
        """
        try:
            result = await self.redis.eval(
                RESERVE, 1, self.key(policy, subject), policy.limit, policy.window_ms
            )
        except (RedisError, OSError):
            raise RateLimitUnavailable from None
        return RateDecision(bool(result[0]), max(1, math.ceil(result[1] / 1000)))

    async def close(self) -> None:
        """Close the application-owned pool after in-flight users have finished."""
        await self.redis.aclose()
