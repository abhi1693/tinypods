"""Authenticated encryption with operator-owned keys and immutable scope context."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from tinypods_core.settings import Settings

MAX_SECRET_BYTES = 262144


class SecretError(Exception):
    """Expose a deliberate status and fixed code without confidential exception text."""

    def __init__(self, code: str, status: int = 422) -> None:
        """Keep callers independent of crypto, parser and transport exceptions."""
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class SecretContext:
    """Bind ciphertext to its logical version and actual cluster association."""

    secret_id: UUID
    version_id: UUID
    version: int
    cluster_id: UUID

    installation_id: UUID | None = None
    namespace: str | None = None

    def associated_data(self) -> bytes:
        """Use a stable, domain-separated encoding; none of these values are secret."""
        if self.installation_id is not None:
            if not self.namespace:
                raise SecretError("secret_scope_missing", 409)
            return json.dumps(
                [
                    "tinypods:application-secret:v1",
                    str(self.secret_id),
                    str(self.version_id),
                    self.version,
                    str(self.cluster_id),
                    str(self.installation_id),
                    self.namespace,
                ],
                separators=(",", ":"),
            ).encode()
        if self.namespace is not None:
            raise SecretError("secret_scope_mismatch", 409)
        return json.dumps(
            [
                "tinypods:cluster-secret:v1",
                str(self.secret_id),
                str(self.version_id),
                self.version,
                str(self.cluster_id),
            ],
            separators=(",", ":"),
        ).encode()


@dataclass(frozen=True)
class Ciphertext:
    """Carry encrypted bytes without placing them in default object representations."""

    key_id: str
    nonce: bytes = field(repr=False)
    payload: bytes = field(repr=False)


class SecretKeyring:
    """Encrypt new writes with the active key while retaining old keys for reads."""

    def __init__(self, settings: Settings) -> None:
        """Copy validated operator key material; no keys are obtained from PostgreSQL."""
        self.active = settings.secret_active_key_id
        self._keys = {
            name: AESGCM(base64.b64decode(value.get_secret_value(), altchars=b"-_", validate=True))
            for name, value in settings.secret_keys.items()
        }

    def encrypt(self, payload: bytes, context: SecretContext) -> Ciphertext:
        """Encrypt original bytes with a fresh nonce and authenticated identity/scope.

        Args:
            payload: Original confidential bytes; encoding is the consumer's concern.
            context: Stable identifiers of the stored logical version and cluster.

        Returns:
            Ciphertext and the external key identifier needed for future resolution.

        Raises:
            SecretError: No active operator key is configured.
        """
        if self.active is None:
            raise SecretError("secret_key_unavailable", 503)
        nonce = os.urandom(12)
        return Ciphertext(
            self.active,
            nonce,
            self._keys[self.active].encrypt(nonce, payload, context.associated_data()),
        )

    def decrypt(self, encrypted: Ciphertext, context: SecretContext) -> bytes:
        """Fail closed on missing keys, changed context or tampered ciphertext."""
        key = self._keys.get(encrypted.key_id)
        if key is None:
            raise SecretError("secret_key_unavailable", 503)
        try:
            return key.decrypt(encrypted.nonce, encrypted.payload, context.associated_data())
        except (InvalidTag, ValueError):
            raise SecretError("secret_integrity_failed", 503) from None
