"""Immutable cache lookup ownership and client configuration shapes."""

from dataclasses import dataclass
from typing import TypedDict

from redis.asyncio.retry import Retry


@dataclass(frozen=True)
class CacheLookup:
    """Carry the response bytes and ownership state returned by a cache lookup.

    A miss grants publication authority only when token is present. Pass the
    generation and token back unchanged: Valkey checks both atomically before
    accepting a fill. A hit has body bytes but does not acquire a fill lock.

    Attributes:
        generation_key: Key storing the namespace's current random generation.
        generation: Generation observed before reading or acquiring a fill lock.
        key: Hashed identity key within that generation.
        lock_key: Expiring ownership key associated with this response.
        token: Unique lock-owner value, or None when this lookup owns no lock.
        body: Size-bounded JSON bytes on a hit, otherwise None.
    """

    generation_key: str
    generation: bytes
    key: str
    lock_key: str
    token: str | None = None
    body: bytes | None = None


class RedisConnectionOptions(TypedDict):
    """Describe the library configuration mapping shared by pool creation and URL overrides."""

    socket_connect_timeout: float
    socket_timeout: float
    retry: Retry
    decode_responses: bool
