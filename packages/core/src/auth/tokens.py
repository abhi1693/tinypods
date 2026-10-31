"""Hash high-entropy bearer material without retaining its plaintext value."""

import hashlib


def digest(token: str) -> str:
    """Return the fixed-size lookup digest of a random session or CSRF token."""
    return hashlib.sha256(token.encode()).hexdigest()
