"""Authenticated byte encryption and hostile kubeconfig validation."""

import base64
import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from secret_fixtures import NEW_KEY, OLD_KEY, TOKEN, document, key_settings, kubeconfig
from tinypods_core.secrets.crypto import Ciphertext, SecretContext, SecretError, SecretKeyring
from tinypods_core.secrets.kubernetes import Kubeconfig, embedded, mapping, named_entries
from tinypods_core.settings import Settings


def test_authenticated_binary_round_trip_and_key_failures() -> None:
    """Scope, logical identity, nonce and tag must all authenticate before resolution."""
    keys = SecretKeyring(key_settings(Settings()))
    context = SecretContext(uuid4(), uuid4(), 1, uuid4())
    original = b"\x00\xffbinary\nsecret payload"
    first = keys.encrypt(original, context)
    second = keys.encrypt(original, context)
    assert first.nonce != second.nonce and first.payload != second.payload
    assert keys.decrypt(first, context) == original
    assert original not in first.payload and original.hex() not in repr(first)
    for changed in (
        replace(context, cluster_id=uuid4()),
        replace(context, secret_id=uuid4()),
        replace(context, version_id=uuid4()),
        replace(context, version=2),
    ):
        with pytest.raises(SecretError, match="integrity_failed"):
            keys.decrypt(first, changed)
    for changed in (
        Ciphertext("old", first.nonce, first.payload[:-1] + bytes([first.payload[-1] ^ 1])),
        Ciphertext("old", b"short", first.payload),
    ):
        with pytest.raises(SecretError, match="integrity_failed"):
            keys.decrypt(changed, context)
    with pytest.raises(SecretError, match="key_unavailable"):
        SecretKeyring(Settings()).encrypt(original, context)
    with pytest.raises(SecretError, match="key_unavailable"):
        SecretKeyring(Settings()).decrypt(first, context)
    rotated = SecretKeyring(key_settings(Settings(), active="new", both=True))
    assert rotated.decrypt(first, context) == original
    assert rotated.encrypt(original, context).key_id == "new"
    assert TOKEN not in repr(Kubeconfig.parse(kubeconfig()))


@pytest.mark.parametrize(
    "updates",
    [
        {"secret_keys": {"old": OLD_KEY}},
        {"secret_active_key_id": "old"},
        {"secret_keys": {"old": OLD_KEY}, "secret_active_key_id": "missing"},
        {"secret_keys": {"bad key": OLD_KEY}, "secret_active_key_id": "bad key"},
        {"secret_keys": {"": OLD_KEY}, "secret_active_key_id": "old"},
        {"secret_keys": {"x" * 65: OLD_KEY}, "secret_active_key_id": "old"},
        {"secret_keys": {"old": "not-base64-secret"}, "secret_active_key_id": "old"},
        {
            "secret_keys": {"old": base64.b64encode(b"too-short").decode()},
            "secret_active_key_id": "old",
        },
    ],
)
def test_invalid_keyrings_fail_settings_without_echo(updates: dict[str, Any]) -> None:
    """Incomplete/invalid keys are startup errors whose displayed text hides values."""
    with pytest.raises(ValidationError) as error:
        Settings(**updates)
    assert OLD_KEY not in str(error.value) and "not-base64-secret" not in str(error.value)


def test_invalid_non_active_key_identifiers_are_rejected() -> None:
    """Validation checks every retained read key, not just the active key name."""
    for name in ("", "bad key", "x" * 65):
        with pytest.raises(ValidationError, match="Invalid secret key identifier"):
            Settings(
                secret_keys={"old": SecretStr(OLD_KEY), name: SecretStr(OLD_KEY)},
                secret_active_key_id="old",
            )


def test_empty_optional_key_setting_and_secret_representations() -> None:
    """Compose can leave the optional feature disabled without affecting ordinary startup."""
    settings = Settings(secret_active_key_id="")
    assert settings.secret_active_key_id is None
    assert OLD_KEY not in repr(key_settings(settings))
    assert NEW_KEY not in repr(key_settings(settings, active="new", both=True))
    assert embedded(None) is None
    with pytest.raises(SecretError):
        mapping([])
    with pytest.raises(SecretError):
        mapping({1: "wrong-key"})
    for value in (
        None,
        [],
        "wrong",
        [{"name": "x", "user": {}}, {"name": "x", "user": {}}],
        [{"name": "", "user": {}}],
        [{"name": 1, "user": {}}],
        [{"name": "x", "user": []}],
    ):
        with pytest.raises(SecretError):
            named_entries(value, "user")
    for value in ("", "%%%", 1):
        with pytest.raises(SecretError):
            embedded(value)


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"x" * 262145,
        b"[",
        b"\xff",
        b"null",
        b"[]",
        b"{1: value}",
        b"x: &x [*x]",
        b"apiVersion: v1\napiVersion: v1",
        b"x: " + b"[" * 40 + b"0" + b"]" * 40,
    ],
)
def test_malformed_yaml_never_exposes_original_input(source: bytes) -> None:
    """Bound input, aliases, duplicate keys and nesting before loading values."""
    with pytest.raises(SecretError, match="invalid_kubeconfig"):
        Kubeconfig.parse(source)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("apiVersion", "v2"),
        ("kind", "Other"),
        ("current-context", "missing"),
        ("current-context", None),
        ("clusters", []),
        ("users", []),
    ],
)
def test_kubeconfig_top_level_validation(kind: str, value: object) -> None:
    """Only a complete named current context may select a credential."""
    config = document()
    config[kind] = value
    with pytest.raises(SecretError):
        Kubeconfig.parse(json.dumps(config).encode())


@pytest.mark.parametrize(
    "server",
    [
        "http://localhost:6443",
        "https://user:password@example.test",
        "https://example.test?token=private",
        "https://example.test/#fragment",
        "https://example.test/path",
        "https://[invalid",
        "https://example.test:99999",
        "https://example.test:0",
        "https://example.test/\n",
        "https://",
        1,
    ],
)
def test_kubeconfig_requires_a_verified_origin(server: object) -> None:
    """Reject insecure transport, credential-bearing URLs and ambiguous endpoint parsing."""
    config = document()
    config["clusters"][0]["cluster"]["server"] = server
    with pytest.raises(SecretError):
        Kubeconfig.parse(json.dumps(config).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("exec", {"command": "must-not-run"}),
        ("auth-provider", {}),
        ("tokenFile", "/must-not-read"),
        ("client-key", "/must-not-read"),
        ("token", ""),
        ("token", "new\nline"),
        ("token", "\u2603"),
        ("token", "x" * 32769),
        ("token", 7),
        ("client-certificate-data", "%%%"),
        ("client-certificate-data", base64.b64encode(b"not-pem").decode()),
    ],
)
def test_unsupported_or_invalid_user_authentication(field: str, value: object) -> None:
    """No external file or command is consulted while interpreting a kubeconfig."""
    config = document()
    config["users"][0]["user"][field] = value
    with pytest.raises(SecretError):
        Kubeconfig.parse(json.dumps(config).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("certificate-authority", "/must-not-read"),
        ("proxy-url", "http://proxy.invalid"),
        ("insecure-skip-tls-verify", True),
        ("certificate-authority-data", "not-base64"),
    ],
)
def test_unsupported_cluster_options(field: str, value: object) -> None:
    """Only embedded trust data or system CAs can establish TLS trust."""
    config = document()
    config["clusters"][0]["cluster"][field] = value
    with pytest.raises(SecretError):
        Kubeconfig.parse(json.dumps(config).encode())


def test_context_references_and_missing_authentication() -> None:
    """Fail on dangling references and unusable client-certificate combinations."""
    for field, value in (
        ("cluster", "missing"),
        ("cluster", None),
        ("user", "missing"),
        ("user", None),
    ):
        config = document()
        config["contexts"][0]["context"][field] = value
        with pytest.raises(SecretError):
            Kubeconfig.parse(json.dumps(config).encode())
    config = document()
    config["users"][0]["user"] = {}
    with pytest.raises(SecretError):
        Kubeconfig.parse(json.dumps(config).encode())
    invalid_authority = Kubeconfig(
        "https://localhost",
        token=SecretStr(TOKEN),
        authority=embedded(base64.b64encode(b"not a certificate").decode()),
    )
    with pytest.raises(SecretError, match="invalid_kubeconfig"):
        invalid_authority.tls_context()
    valid = Kubeconfig.parse(kubeconfig("https://[::1]:6443/"))
    assert valid.server == "https://[::1]:6443"
    assert valid.tls_context().check_hostname


def test_excessive_yaml_parser_nesting_and_unsafe_tags() -> None:
    """Even parser recursion and Python-specific tags become fixed input errors."""
    for source in (b"[" * 1500 + b"0" + b"]" * 1500, b"!!python/object/apply:must_not_execute []"):
        with pytest.raises(SecretError, match="invalid_kubeconfig"):
            Kubeconfig.parse(source)


def test_confidential_dto_validation_hides_utf8_and_length_inputs() -> None:
    """Write validation never needs to print the original confidential document."""
    from tinypods_api.secrets.contracts import ClusterSecretWrite

    for payload in (TOKEN + chr(0xD800), TOKEN + "\u2603" * 100000, TOKEN + "x" * 262144):
        with pytest.raises(ValidationError) as error:
            ClusterSecretWrite(cluster_id=uuid4(), name="Pi", kubeconfig=SecretStr(payload))
        assert TOKEN not in str(error.value)
