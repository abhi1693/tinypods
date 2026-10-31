"""Shared HTTP headers, media types and probe paths."""

from typing import Final

REQUEST_ID_HEADER: Final = "x-request-id"


REQUEST_ID_HEADER_BYTES: Final = REQUEST_ID_HEADER.encode("ascii")


CONTENT_TYPE_HEADER: Final = "content-type"


CONTENT_LENGTH_HEADER: Final = "content-length"


LIVENESS_PATH: Final = "/healthz"


READINESS_PATH: Final = "/readyz"


CACHE_CONTROL_HEADER: Final = "cache-control"


JSON_MEDIA_TYPE: Final = "application/json"


NO_STORE: Final = "no-store"


QUIET_PROBE_PATHS: Final = frozenset({LIVENESS_PATH, READINESS_PATH})
