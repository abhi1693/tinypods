"""Immutable transport defaults owned by the response-cache adapter."""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class CacheTransport:
    """Keep URL overrides from enlarging the cache adapter's network resource bounds.

    Attributes:
        socket_timeout: Connection and operation timeout in seconds.
        max_connections: Maximum connections owned by one cache process.
    """

    socket_timeout: float = 0.2
    max_connections: int = 32


CACHE_TRANSPORT: Final = CacheTransport()
