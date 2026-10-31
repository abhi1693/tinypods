"""Async Valkey pool ownership, cache generations and conditional publication."""

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.maint_notifications import MaintNotificationsConfig
from tinypods_core import __version__
from tinypods_core.cache.codec import json_body
from tinypods_core.cache.constants import CACHE_TRANSPORT
from tinypods_core.cache.models import CacheLookup, RedisConnectionOptions
from tinypods_core.settings import Settings

logger = logging.getLogger("tinypods.cache")


LOCK_SECONDS = 30


RETRY_SECONDS = 5


PUBLISH = """
if redis.call('GET', KEYS[1]) == ARGV[1]
   and redis.call('GET', KEYS[2]) == ARGV[2] then
    redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[4])
    return 1
end
return 0
"""


DELETE_IF_MATCH = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class CacheUnavailable(Exception):
    """Report unavailable cache storage or an unusable generation identifier.

    Cache consumers can catch this exception and read from the authoritative
    source. It does not include the connection URL or the underlying error text.
    Cancellation and programming errors are not converted into this exception.
    """


class ResponseCache:
    """Own an async Valkey pool and generation-scoped public response entries.

    Use one instance within its application's event loop and await every storage
    operation. The client connects lazily, uses bounded socket deadlines and has
    no retry loop. A storage error opens a short circuit breaker shared by this
    instance; requests can then read through without repeatedly contacting an
    unavailable server. Keys are namespaced separately from durable queue data.

    The application lifespan owns this object and must await close() at shutdown.
    JSON decoding/hashing remain synchronous CPU work bounded by the body limit
    and fixed-size cache keys; this class performs no synchronous network I/O.
    """

    def __init__(self, settings: Settings) -> None:
        """Configure a lazily connected async pool and a versioned storage namespace.

        Args:
            settings: Endpoint, namespace and payload limits. The cache-specific URL
                takes precedence over the shared Valkey URL.

        Returns:
            None. The instance owns an unopened pool until its first awaited operation.

        Raises:
            ValueError: No endpoint is configured or its client options are invalid.
        """
        configured = settings.cache_valkey_url or settings.valkey_url
        if configured is None:
            raise ValueError("A Valkey URL is required for the response cache")
        options: RedisConnectionOptions = {
            "socket_connect_timeout": CACHE_TRANSPORT.socket_timeout,
            "socket_timeout": CACHE_TRANSPORT.socket_timeout,
            "retry": Retry(NoBackoff(), 0),
            "decode_responses": False,
        }
        self.redis = Redis.from_url(
            configured.get_secret_value(),
            max_connections=CACHE_TRANSPORT.max_connections,
            maint_notifications_config=MaintNotificationsConfig(enabled=False),
            **options,
        )
        # URL query options cannot enlarge I/O bounds or change binary response types.
        self.redis.connection_pool.connection_kwargs.update(options)
        source = (
            settings.database_url.get_secret_value() if settings.database_url else "unconfigured"
        )
        identity = hashlib.sha256(source.encode()).hexdigest()[:16]
        self.namespace = f"{settings.cache_namespace}:{__version__}:{identity}"
        self.redis.connection_pool.max_connections = CACHE_TRANSPORT.max_connections
        self.max_bytes = settings.cache_max_bytes
        self.blocked_until = 0.0

    async def _run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Await an operation unless the local failure circuit breaker is open.

        The breaker state belongs to the application's event loop. Updates contain
        no await, so concurrent requests cannot interleave a partial state update.
        Warnings contain the error type, not server messages or connection URLs.

        Args:
            operation: Factory returning the async Valkey operation to execute.

        Returns:
            The operation's result without additional conversion.

        Raises:
            CacheUnavailable: The breaker is open or a Redis/socket operation fails.
        """
        if self.blocked_until > time.monotonic():
            raise CacheUnavailable
        try:
            return await operation()
        except (RedisError, OSError) as exc:
            warn = self.blocked_until <= time.monotonic()
            self.blocked_until = time.monotonic() + RETRY_SECONDS
            if warn:
                logger.warning("Cache unavailable", extra={"error_type": type(exc).__name__})
            raise CacheUnavailable from None

    async def lookup(self, identity: str) -> CacheLookup:
        """Read a generation-scoped response or acquire an expiring fill token.

        Missing generations use random SET NX initialization, including after
        eviction. Invalid-size, malformed or non-finite response bytes are misses.
        A miss without a token belongs to another loader; callers may briefly wait
        or render without publishing. Returned owners must release their token
        after rendering. Abandoned acquisitions also expire on the server.

        Args:
            identity: Canonical route/path/query representation to hash into a key.

        Returns:
            Response bytes on a hit, or lookup state with an optional owner token.

        Raises:
            CacheUnavailable: Storage fails or the generation value is unusable.
        """

        async def operation() -> CacheLookup:
            """Read generation/body state and claim a miss without resetting another owner.

            Returns:
                Lookup state retaining the observed generation and any acquired token.

            Raises:
                CacheUnavailable: The stored generation is not a valid random identifier.
            """
            generation_key = f"{self.namespace}:public:generation"
            generation = await self.redis.get(generation_key)
            # Random generations prevent stale keys from reappearing after generation eviction.
            if generation is None:
                await self.redis.set(generation_key, uuid4().hex, nx=True)
                generation = await self.redis.get(generation_key)
            if (
                not isinstance(generation, bytes)
                or len(generation) != 32
                or any(byte not in b"0123456789abcdef" for byte in generation)
            ):
                raise CacheUnavailable
            digest = hashlib.sha256(identity.encode()).hexdigest()
            key = f"{self.namespace}:public:{generation.decode('ascii')}:{digest}"
            lock_key = f"{key}:lock"
            body = await self.redis.get(key)
            if body is not None:
                body = json_body(body, self.max_bytes)
                if body is None:
                    logger.warning("Invalid cache entry")
            token = None
            if body is None:
                candidate = uuid4().hex
                # Server expiry bounds abandoned ownership when cleanup cannot reach Valkey.
                if await self.redis.set(lock_key, candidate, nx=True, ex=LOCK_SECONDS):
                    token = candidate
            return CacheLookup(generation_key, generation, key, lock_key, token, body)

        return await self._run(operation)

    async def publish(self, lookup: CacheLookup, body: bytes, ttl: int) -> bool:
        """Conditionally store a rendered response while ownership is still valid.

        Generation comparison, lock comparison and publication run atomically in
        Valkey. An invalidated or expired loader cannot replace a newer response.
        The caller still owns cleanup after this method returns.

        Args:
            lookup: Unmodified lookup state and owner token from a prior miss.
            body: Serialized public JSON response bytes.
            ttl: Positive lifetime in seconds for a successfully published response.

        Returns:
            True if stored; False for missing ownership, stale generation or size overflow.

        Raises:
            ValueError: A publishable response is given a non-positive TTL.
            CacheUnavailable: A Valkey operation fails or the breaker is open.
        """
        if lookup.token is None or len(body) > self.max_bytes:
            return False
        if ttl <= 0:
            raise ValueError("Cache TTL must be positive")
        token = lookup.token
        return bool(
            await self._run(
                lambda: self.redis.eval(
                    PUBLISH,
                    3,
                    lookup.generation_key,
                    lookup.lock_key,
                    lookup.key,
                    lookup.generation,
                    token,
                    body,
                    ttl,
                )
            )
        )

    async def release(self, lookup: CacheLookup) -> None:
        """Remove a fill lock only while the supplied token remains its owner.

        Args:
            lookup: Lookup whose token is being released; no token makes this a no-op.

        Returns:
            None. A replacement owner's lock is preserved.

        Raises:
            CacheUnavailable: Storage is unavailable during the ownership comparison.
        """
        token = lookup.token
        if token:
            await self._run(lambda: self.redis.eval(DELETE_IF_MATCH, 1, lookup.lock_key, token))

    async def discard(self, lookup: CacheLookup) -> None:
        """Remove an invalid observed response only if its stored bytes have not changed.

        Args:
            lookup: Observation containing the invalid body and its exact response key.

        Returns:
            None. A newer publisher's value or a lookup without a body is unchanged.

        Raises:
            CacheUnavailable: Storage is unavailable during conditional deletion.
        """
        body = lookup.body
        if body is not None:
            await self._run(lambda: self.redis.eval(DELETE_IF_MATCH, 1, lookup.key, body))

    async def invalidate(self) -> None:
        """Rotate the namespace generation after an authoritative change commits.

        New lookups use the new generation. Earlier fills cannot publish into it,
        and old response keys expire naturally. Queue keys are never changed.
        Callers must not invalidate on rollback; failed invalidation leaves TTL
        as the fallback freshness bound.

        Returns:
            None. Subsequent lookups observe a new random generation.

        Raises:
            CacheUnavailable: The new generation cannot be stored.
        """
        await self._run(lambda: self.redis.set(f"{self.namespace}:public:generation", uuid4().hex))

    async def close(self) -> None:
        """Close the owned async pool after the application stops cache operations.

        Returns:
            None. Open sockets are disconnected; stored responses and queue data remain.
        """
        await self.redis.aclose()
