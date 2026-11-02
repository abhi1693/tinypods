"""Controlled kubeconfigs and public fixture keys; never operator credentials."""

import base64
import json
from typing import Any

from pydantic import SecretStr
from tinypods_core.settings import Settings

TOKEN = "fixture-private-bearer-do-not-log"
OLD_KEY = base64.urlsafe_b64encode(b"o" * 32).decode()
NEW_KEY = base64.urlsafe_b64encode(b"n" * 32).decode()


def key_settings(settings: Settings, *, active: str = "old", both: bool = False) -> Settings:
    """Validate a deterministic fixture-only keyring on an isolated database."""
    keys = {"old": SecretStr(OLD_KEY)}
    if both:
        keys["new"] = SecretStr(NEW_KEY)
    return Settings.model_validate(
        {**settings.model_dump(), "secret_keys": keys, "secret_active_key_id": active}
    )


def document(server: str = "https://kubernetes.example.test", token: str = TOKEN) -> dict[str, Any]:
    """Return a minimal current-context kubeconfig with a controlled bearer token."""
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "selected",
        "clusters": [{"name": "target", "cluster": {"server": server}}],
        "contexts": [{"name": "selected", "context": {"cluster": "target", "user": "operator"}}],
        "users": [{"name": "operator", "user": {"token": token}}],
    }


def kubeconfig(server: str = "https://kubernetes.example.test", token: str = TOKEN) -> bytes:
    """Encode original bytes without any operator file or external network access."""
    return json.dumps(document(server, token)).encode()
