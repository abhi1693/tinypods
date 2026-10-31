"""HTTP response-cache outcomes, policy names and bounded fill timing."""

from enum import StrEnum
from typing import Final

from tinypods_api.http.constants import CONTENT_LENGTH_HEADER, CONTENT_TYPE_HEADER, NO_STORE


class CacheOutcome(StrEnum):
    """Public X-Cache outcomes; changing values would change the HTTP contract."""

    HIT = "HIT"
    MISS = "MISS"
    BYPASS = "BYPASS"


CACHE_OUTCOME_HEADER: Final = "x-cache"


CACHE_REASON_HEADER: Final = "x-cache-bypass-reason"


CACHE_POLICY_EXTENSION: Final = "x-tinypods-cache"


CACHE_WAIT_SECONDS: Final = 1.0


CACHE_POLL_SECONDS: Final = 0.05


MAX_QUERY_BYTES: Final = 4096


STORABLE_RESPONSE_HEADERS: Final = frozenset({CONTENT_TYPE_HEADER, CONTENT_LENGTH_HEADER})


REQUEST_BYPASS_DIRECTIVES: Final = frozenset({"no-cache", NO_STORE})
