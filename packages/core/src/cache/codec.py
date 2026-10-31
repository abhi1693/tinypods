"""Complete bounded cache-body validation at the storage boundary."""

import json
from typing import NoReturn


def reject_constant(value: str) -> NoReturn:
    """Reject a non-standard numeric token during JSON decoding.

    Args:
        value: NaN or Infinity token supplied by the JSON decoder.

    Raises:
        ValueError: Always; these tokens are not valid cached JSON numbers.
    """
    raise ValueError("Non-finite JSON values are not cacheable")


def json_body(value: object, max_bytes: int) -> bytes | None:
    """Accept a bounded byte sequence containing finite JSON.

    Args:
        value: Stored response value returned by Valkey.
        max_bytes: Maximum serialized response size accepted by this cache.

    Returns:
        The original bytes when valid, otherwise None. DTO validation belongs
        to the route wrapper that knows the response model.
    """
    if not isinstance(value, bytes) or len(value) > max_bytes:
        return None
    try:
        json.loads(value, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value
