"""Configurable async encryption providers independent of deployment technology."""

import json
from typing import Protocol

import anyio
from pydantic import ValidationError
from tinypods_core.secrets.crypto import Ciphertext, SecretContext, SecretError, SecretKeyring
from tinypods_core.settings import Settings


class EncryptionProvider(Protocol):
    """Keep persistence independent of where keys are held or cryptography runs."""

    @property
    def active(self) -> str | None:
        """Identify the configured key for new writes, without returning key material."""
        ...

    async def encrypt(self, payload: bytes, context: SecretContext) -> Ciphertext:
        """Protect original bytes under authenticated identity and scope."""
        ...

    async def decrypt(self, encrypted: Ciphertext, context: SecretContext) -> bytes:
        """Resolve bytes only after authenticating identity and scope."""
        ...


class EnvironmentProvider:
    """Use explicitly configured environment keys without imposing file mounts."""

    def __init__(self, settings: Settings) -> None:
        """Retain validated, masked configuration through the local encryption primitive."""
        self.keyring = SecretKeyring(settings)
        self.active = self.keyring.active

    async def encrypt(self, payload: bytes, context: SecretContext) -> Ciphertext:
        """Perform bounded local CPU work without network or filesystem access."""
        return self.keyring.encrypt(payload, context)

    async def decrypt(self, encrypted: Ciphertext, context: SecretContext) -> bytes:
        """Authenticate and decrypt locally; no plaintext cache is retained."""
        return self.keyring.decrypt(encrypted, context)


class FileProvider:
    """Read an operator-chosen key file asynchronously without caching its contents.

    The operator owns filesystem permissions and deployment-specific delivery.
    Docker, Kubernetes, service managers and direct execution use the same provider.
    No file path or mount is inferred from the surrounding runtime.
    """

    def __init__(self, settings: Settings) -> None:
        """Retain configuration only; metadata reads do not open the key file."""
        self.settings = settings
        self.active = settings.secret_active_key_id

    async def _keyring(self) -> SecretKeyring:
        """Bound the key file and suppress confidential parser/OS exception details."""
        path = self.settings.secret_key_file
        assert path is not None
        try:
            # AnyIO's async file API owns the filesystem worker boundary; file
            # reads never execute synchronously on the API event loop.
            async with await anyio.open_file(path, "rb") as stream:
                contents = await stream.read(65537)
            if len(contents) > 65536:
                raise SecretError("secret_key_unavailable", 503)
            values = json.loads(contents)
            local = Settings.model_validate(
                {
                    **self.settings.model_dump(),
                    "secret_key_provider": "environment",
                    "secret_key_file": None,
                    "secret_keys": values,
                }
            )
            return SecretKeyring(local)
        except (OSError, ValueError, UnicodeError, RecursionError, ValidationError):
            raise SecretError("secret_key_unavailable", 503) from None

    async def encrypt(self, payload: bytes, context: SecretContext) -> Ciphertext:
        """Read the configured key source and protect bytes without retaining a key cache."""
        return (await self._keyring()).encrypt(payload, context)

    async def decrypt(self, encrypted: Ciphertext, context: SecretContext) -> bytes:
        """Load keys at resolution time; missing/unreadable files never trigger a fallback."""
        return (await self._keyring()).decrypt(encrypted, context)


def encryption_provider(settings: Settings) -> EncryptionProvider:
    """Select trusted built-ins from configuration, without arbitrary module imports."""
    return (
        FileProvider(settings)
        if settings.secret_key_provider == "file"
        else EnvironmentProvider(settings)
    )
