"""Bound Argon2id CPU and memory work outside the asynchronous event loop."""

from functools import partial

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

HASHER = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)
HASH_LIMITER = anyio.CapacityLimiter(2)


async def hash_password(password: str) -> str:
    """Hash a password with a random salt, at most two operations per process."""
    return await anyio.to_thread.run_sync(HASHER.hash, password, limiter=HASH_LIMITER)


async def verify_password(encoded: str | None, password: str) -> bool:
    """Verify safely; missing accounts still pay one password-hash operation."""
    if encoded is None:
        await hash_password(password)
        return False
    try:
        return await anyio.to_thread.run_sync(
            partial(HASHER.verify, encoded, password), limiter=HASH_LIMITER
        )
    except (VerificationError, InvalidHashError):
        return False
