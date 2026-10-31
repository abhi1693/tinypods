"""Optional response caching; PostgreSQL and queue state remain authoritative."""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn
from uuid import uuid4

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.maint_notifications import MaintNotificationsConfig
from redis.retry import Retry
from tinypods_core import __version__
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
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def reject_constant(value: str) -> NoReturn:
    raise ValueError("Non-finite JSON values are not cacheable")


class CacheUnavailable(Exception):
    """Read through to the source; optional cache failure must not fail the request."""


@dataclass(frozen=True)
class CacheLookup:
    generation_key: str
    generation: bytes
    key: str
    lock_key: str
    token: str | None = None
    body: bytes | None = None


class ResponseCache:
    """Thread-safe synchronous client. Async callers must offload its methods."""

    def __init__(self, settings: Settings) -> None:
        configured = settings.cache_valkey_url or settings.valkey_url
        if configured is None:
            raise ValueError("A Valkey URL is required for the response cache")
        self.redis = Redis.from_url(
            configured.get_secret_value(),
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
            max_connections=32,
            decode_responses=False,
            maint_notifications_config=MaintNotificationsConfig(enabled=False),
        )
        # URL query options must not enlarge I/O bounds or turn binary responses into strings.
        self.redis.connection_pool.connection_kwargs.update(
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
            decode_responses=False,
        )
        source = (
            settings.database_url.get_secret_value() if settings.database_url else "unconfigured"
        )
        identity = hashlib.sha256(source.encode()).hexdigest()[:16]
        self.namespace = f"{settings.cache_namespace}:{__version__}:{identity}"
        self.redis.connection_pool.max_connections = 32
        self.max_bytes = settings.cache_max_bytes
        self.blocked_until = 0.0
        self.guard = threading.Lock()

    def _run[T](self, operation: Callable[[], T]) -> T:
        with self.guard:
            if self.blocked_until > time.monotonic():
                raise CacheUnavailable
        try:
            return operation()
        except (RedisError, OSError) as exc:
            with self.guard:
                warn = self.blocked_until <= time.monotonic()
                self.blocked_until = time.monotonic() + RETRY_SECONDS
            if warn:
                logger.warning("Cache unavailable", extra={"error_type": type(exc).__name__})
            raise CacheUnavailable from None

    def lookup(self, identity: str) -> CacheLookup:
        def operation() -> CacheLookup:
            generation_key = f"{self.namespace}:public:generation"
            generation = self.redis.get(generation_key)
            if generation is None:
                self.redis.set(generation_key, uuid4().hex, nx=True)
                generation = self.redis.get(generation_key)
            if (
                not isinstance(generation, bytes)
                or len(generation) != 32
                or any(byte not in b"0123456789abcdef" for byte in generation)
            ):
                raise CacheUnavailable
            digest = hashlib.sha256(identity.encode()).hexdigest()
            key = f"{self.namespace}:public:{generation.decode('ascii')}:{digest}"
            lock_key = f"{key}:lock"
            body = self.redis.get(key)
            if body is not None:
                try:
                    if not isinstance(body, bytes) or len(body) > self.max_bytes:
                        raise ValueError("Invalid response size")
                    json.loads(body, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    logger.warning("Invalid cache entry")
                    body = None
            token = None
            if body is None:
                candidate = uuid4().hex
                if self.redis.set(lock_key, candidate, nx=True, ex=LOCK_SECONDS):
                    token = candidate
            return CacheLookup(generation_key, generation, key, lock_key, token, body)

        return self._run(operation)

    def publish(self, lookup: CacheLookup, body: bytes, ttl: int) -> bool:
        if lookup.token is None or len(body) > self.max_bytes:
            return False
        if ttl <= 0:
            raise ValueError("Cache TTL must be positive")
        token = lookup.token
        return bool(
            self._run(
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

    def release(self, lookup: CacheLookup) -> None:
        token = lookup.token
        if token:
            self._run(lambda: self.redis.eval(RELEASE, 1, lookup.lock_key, token))

    def discard(self, lookup: CacheLookup) -> None:
        """Remove a corrupt observed body without deleting a newer publisher's value."""
        body = lookup.body
        if body is not None:
            self._run(lambda: self.redis.eval(RELEASE, 1, lookup.key, body))

    def invalidate(self) -> None:
        """Rotate only this response namespace after a successful source commit."""
        self._run(lambda: self.redis.set(f"{self.namespace}:public:generation", uuid4().hex))

    def close(self) -> None:
        self.redis.close()
