"""Authenticated server-only encryption for provider refresh state and operator key rotation."""

import json
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from tinypods_core.auth.contracts import AuthError
from tinypods_core.settings import Settings


class RefreshState(BaseModel):
    """Keep bearer material out of representations and validate the encrypted payload shape."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    grant_id: UUID
    refresh_token: SecretStr
    nonce: str
    auth_time: float | None = None


class RefreshCipher:
    """Encrypt with the first operator key and accept previous keys during rotation."""

    def __init__(self, settings: Settings) -> None:
        """Retain only configured authenticated-encryption primitives."""
        if not settings.zitadel_refresh_encryption_keys:
            raise AuthError(503)
        self.cipher = MultiFernet(
            [
                Fernet(key.get_secret_value().encode("ascii"))
                for key in settings.zitadel_refresh_encryption_keys
            ]
        )

    def encrypt(self, state: RefreshState) -> str:
        """Encrypt explicitly selected secrets; public model serialization redacts them."""
        payload = json.dumps(
            {
                "grant_id": str(state.grant_id),
                "refresh_token": state.refresh_token.get_secret_value(),
                "nonce": state.nonce,
                "auth_time": state.auth_time,
            }
        ).encode()
        return self.cipher.encrypt(payload).decode("ascii")

    def decrypt(self, ciphertext: str, grant_id: UUID) -> RefreshState:
        """Authenticate stored state and reject ciphertext copied from another grant."""
        try:
            state = RefreshState.model_validate_json(
                self.cipher.decrypt(ciphertext.encode("ascii"))
            )
        except (InvalidToken, ValidationError, ValueError, UnicodeError):
            raise AuthError(401) from None
        if state.grant_id != grant_id:
            raise AuthError(401)
        return state
