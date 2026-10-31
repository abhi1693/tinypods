"""Immutable HTTP protocol values used by API middleware and response caching."""

from enum import StrEnum
from typing import Final


class CacheOutcome(StrEnum):
    """Public X-Cache outcomes; changing values would change the HTTP contract."""

    HIT = "HIT"
    MISS = "MISS"
    BYPASS = "BYPASS"


REQUEST_ID_HEADER: Final = "x-request-id"
REQUEST_ID_HEADER_BYTES: Final = REQUEST_ID_HEADER.encode("ascii")
CONTENT_TYPE_HEADER: Final = "content-type"
CONTENT_LENGTH_HEADER: Final = "content-length"
LIVENESS_PATH: Final = "/healthz"
READINESS_PATH: Final = "/readyz"
CACHE_CONTROL_HEADER: Final = "cache-control"
CACHE_OUTCOME_HEADER: Final = "x-cache"
CACHE_REASON_HEADER: Final = "x-cache-bypass-reason"
JSON_MEDIA_TYPE: Final = "application/json"
NO_STORE: Final = "no-store"
CACHE_POLICY_EXTENSION: Final = "x-tinypods-cache"
CACHE_WAIT_SECONDS: Final = 1.0
CACHE_POLL_SECONDS: Final = 0.05
MAX_QUERY_BYTES: Final = 4096
STORABLE_RESPONSE_HEADERS: Final = frozenset({CONTENT_TYPE_HEADER, CONTENT_LENGTH_HEADER})
REQUEST_BYPASS_DIRECTIVES: Final = frozenset({"no-cache", NO_STORE})
QUIET_PROBE_PATHS: Final = frozenset({LIVENESS_PATH, READINESS_PATH})
