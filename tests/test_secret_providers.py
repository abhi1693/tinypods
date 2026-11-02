"""Async key source selection and confidential file failures."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from secret_fixtures import NEW_KEY, OLD_KEY, key_settings
from tinypods_core.secrets.crypto import SecretContext, SecretError
from tinypods_core.secrets.providers import encryption_provider
from tinypods_core.settings import Settings


@pytest.mark.anyio
async def test_file_provider_roundtrip_and_reload(tmp_path: Path) -> None:
    """Replacement key files take effect without retaining stale key material."""
    path = tmp_path / "keys.json"
    settings = Settings(
        secret_key_provider="file", secret_key_file=path, secret_active_key_id="old"
    )
    provider = encryption_provider(settings)
    assert provider.active == "old"
    context = SecretContext(uuid4(), uuid4(), 1, uuid4())
    path.write_text(json.dumps({"old": OLD_KEY}))
    encrypted = await provider.encrypt(b"private fixture", context)
    assert await provider.decrypt(encrypted, context) == b"private fixture"
    path.write_text(json.dumps({"old": NEW_KEY}))
    with pytest.raises(SecretError, match="integrity_failed"):
        await provider.decrypt(encrypted, context)


@pytest.mark.anyio
async def test_environment_provider_roundtrip() -> None:
    """The environment backend implements the same awaitable provider contract."""
    provider = encryption_provider(key_settings(Settings()))
    context = SecretContext(uuid4(), uuid4(), 1, uuid4())
    encrypted = await provider.encrypt(b"fixture", context)
    assert await provider.decrypt(encrypted, context) == b"fixture"


@pytest.mark.anyio
async def test_file_provider_failures_are_confidential(tmp_path: Path) -> None:
    """Missing, malformed and oversized files fail closed without exposing contents."""
    path = tmp_path / "keys.json"
    provider = encryption_provider(
        Settings(secret_key_provider="file", secret_key_file=path, secret_active_key_id="old")
    )
    context = SecretContext(uuid4(), uuid4(), 1, uuid4())
    for contents in (
        None,
        b"private malformed",
        b"x" * 65537,
        b"[]",
        b'{"old":"private-invalid-key"}',
        bytes([255]),
    ):
        if contents is not None:
            path.write_bytes(contents)
        with pytest.raises(SecretError, match="secret_key_unavailable") as error:
            await provider.encrypt(b"fixture", context)
        assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "updates",
    [
        {"secret_key_provider": "unknown"},
        {"secret_key_provider": "file", "secret_active_key_id": "old"},
        {
            "secret_key_provider": "file",
            "secret_key_file": "relative.json",
            "secret_active_key_id": "old",
        },
        {"secret_key_provider": "file", "secret_key_file": "/keys.json"},
        {
            "secret_key_provider": "file",
            "secret_key_file": "/keys.json",
            "secret_active_key_id": "old",
            "secret_keys": {"old": OLD_KEY},
        },
        {"secret_key_file": "/keys.json"},
    ],
)
def test_provider_configuration_rejects_ambiguous_sources(updates: dict[str, object]) -> None:
    """Operators must explicitly select one complete key source."""
    with pytest.raises(ValidationError):
        Settings.model_validate(updates)
