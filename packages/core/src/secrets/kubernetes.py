"""Bounded kubeconfig parsing and native async Kubernetes identity verification."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import ssl
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import anyio
import httpx2
import yaml  # type: ignore[import-untyped]
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import SecretBytes, SecretStr
from tinypods_core.secrets.crypto import MAX_SECRET_BYTES, SecretError


@dataclass(frozen=True)
class Kubeconfig:
    """Hold one selected context without commands, file references or unsafe TLS."""

    server: str = field(repr=False)
    token: SecretStr | None = field(default=None, repr=False)
    certificate: SecretBytes | None = field(default=None, repr=False)
    key: SecretBytes | None = field(default=None, repr=False)
    authority: SecretBytes | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, payload: bytes) -> Kubeconfig:
        """Validate a bounded, self-contained kubeconfig without invoking plugins.

        Args:
            payload: Original UTF-8 YAML or JSON document supplied by an owner.

        Returns:
            Selected endpoint and embedded authentication material.

        Raises:
            SecretError: Malformed, ambiguous or unsupported configuration.
        """
        if not payload or len(payload) > MAX_SECRET_BYTES:
            raise SecretError("invalid_kubeconfig")
        try:
            # Reject aliases before construction: a small source document must not
            # expand into an arbitrarily large or recursive object graph.
            if any(
                isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
                for token in yaml.scan(payload)
            ):
                raise SecretError("invalid_kubeconfig")
            validate_nodes(yaml.compose(payload))
            document = mapping(yaml.safe_load(payload))
        except (yaml.YAMLError, UnicodeError, ValueError, RecursionError):
            raise SecretError("invalid_kubeconfig") from None
        if document.get("apiVersion") != "v1" or document.get("kind") != "Config":
            raise SecretError("invalid_kubeconfig")
        clusters = named_entries(document.get("clusters"), "cluster")
        users = named_entries(document.get("users"), "user")
        contexts = named_entries(document.get("contexts"), "context")
        for value in clusters.values():
            if (
                set(value) - {"server", "certificate-authority-data", "insecure-skip-tls-verify"}
                or value.get("insecure-skip-tls-verify", False) is not False
            ):
                raise SecretError("unsupported_kubeconfig")
        for value in users.values():
            if set(value) - {"token", "client-certificate-data", "client-key-data"}:
                raise SecretError("unsupported_kubeconfig")
        selected = document.get("current-context")
        if not isinstance(selected, str) or selected not in contexts:
            raise SecretError("invalid_kubeconfig")
        context = contexts[selected]
        cluster_name, user_name = context.get("cluster"), context.get("user")
        if (
            not isinstance(cluster_name, str)
            or cluster_name not in clusters
            or not isinstance(user_name, str)
            or user_name not in users
        ):
            raise SecretError("invalid_kubeconfig")
        cluster, user = clusters[cluster_name], users[user_name]
        server = cluster.get("server")
        if not isinstance(server, str) or any(
            character.isspace() or ord(character) < 33 for character in server
        ):
            raise SecretError("invalid_kubeconfig")
        try:
            url = urlsplit(server)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not url.path.strip("/")
                and (url.port is None or 1 <= url.port <= 65535)
            )
        except ValueError:
            raise SecretError("invalid_kubeconfig") from None
        if not valid:
            raise SecretError("unsupported_kubeconfig")
        token = user.get("token")
        if token is not None and (
            not isinstance(token, str)
            or not token
            or len(token) > 32768
            or any(character.isspace() for character in token)
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            raise SecretError("invalid_kubeconfig")
        certificate = embedded(user.get("client-certificate-data"))
        key = embedded(user.get("client-key-data"))
        if bool(certificate) != bool(key) or bool(token) == bool(certificate):
            raise SecretError("unsupported_kubeconfig")
        return cls(
            server.rstrip("/"),
            SecretStr(token) if token is not None else None,
            certificate,
            key,
            embedded(cluster.get("certificate-authority-data")),
        )

    def tls_context(self) -> ssl.SSLContext:
        """Build verified TLS and remove the private certificate-loading files immediately.

        Python's SSL loader accepts client certificates through files. The small
        synchronous setup boundary owns a private directory and exclusive 0600
        files, removes them before network I/O, and never logs their contents.
        Plaintext keys remain in process memory for the SSL context's lifetime.
        """
        try:
            context = ssl.create_default_context()
            if self.authority is not None:
                context.load_verify_locations(
                    cadata=self.authority.get_secret_value().decode("ascii")
                )
            if self.certificate is not None and self.key is not None:
                # Reject password-protected PEM before OpenSSL can attempt an
                # interactive password prompt in an API process.
                load_pem_private_key(self.key.get_secret_value(), password=None)
                with tempfile.TemporaryDirectory(prefix="tinypods-tls-") as directory:
                    paths = []
                    for name, value in (("client.crt", self.certificate), ("client.key", self.key)):
                        path = Path(directory) / name
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(value.get_secret_value())
                        paths.append(path)
                    context.load_cert_chain(paths[0], paths[1])
            return context
        except (ssl.SSLError, UnicodeError, OSError, ValueError, TypeError, UnsupportedAlgorithm):
            raise SecretError("invalid_kubeconfig") from None


def validate_nodes(node: object, depth: int = 0) -> None:
    """Reject duplicate YAML keys and excessive nesting before constructing values."""
    if depth > 32:
        raise SecretError("invalid_kubeconfig")
    if isinstance(node, yaml.nodes.MappingNode):
        keys: set[str] = set()
        for key, value in node.value:
            if not isinstance(key, yaml.nodes.ScalarNode) or key.value in keys:
                raise SecretError("invalid_kubeconfig")
            keys.add(key.value)
            validate_nodes(value, depth + 1)
    elif isinstance(node, yaml.nodes.SequenceNode):
        for item in node.value:
            validate_nodes(item, depth + 1)


def mapping(value: object) -> Mapping[str, object]:
    """Accept mappings with string keys without traversing arbitrary YAML objects."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SecretError("invalid_kubeconfig")
    return cast(Mapping[str, object], value)


def named_entries(value: object, content: str) -> dict[str, Mapping[str, object]]:
    """Resolve unique names so duplicate contexts cannot silently select credentials."""
    if not isinstance(value, list) or not value or len(value) > 1000:
        raise SecretError("invalid_kubeconfig")
    result: dict[str, Mapping[str, object]] = {}
    for item in value:
        entry = mapping(item)
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise SecretError("invalid_kubeconfig")
        result[name] = mapping(entry.get(content))
    return result


def embedded(value: object) -> SecretBytes | None:
    """Decode bounded embedded data; external paths are never opened."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SecretError("invalid_kubeconfig")
    try:
        return SecretBytes(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error):
        raise SecretError("invalid_kubeconfig") from None


@dataclass(frozen=True)
class ConnectionResult:
    """Return fixed diagnostic categories, never upstream bodies or credentials."""

    connected: bool
    reason: str
    kubernetes_version: str | None = None


class KubernetesClient:
    """Test one selected cluster using verified native async HTTP with bounded replies."""

    def __init__(
        self, *, timeout: float = 10, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        """Retain a bounded deadline and an optional controlled-test transport."""
        self.timeout = timeout
        self.transport = transport

    async def _json(
        self, client: httpx2.AsyncClient, method: str, path: str, body: dict[str, str] | None = None
    ) -> Mapping[str, object]:
        """Read a bounded response without copying arbitrary upstream errors into logs."""
        async with client.stream(method, path, json=body) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise SecretError("invalid_response")
            if response.status_code in (401, 403):
                raise SecretError("unauthorized" if response.status_code == 401 else "forbidden")
            if response.status_code not in (200, 201):
                raise SecretError("invalid_response")
            payload = bytearray()
            async for chunk in response.aiter_raw(chunk_size=16384):
                payload.extend(chunk)
                if len(payload) > 65536:
                    raise SecretError("invalid_response")
            try:
                return mapping(json.loads(payload))
            except (ValueError, UnicodeError, RecursionError, SecretError):
                raise SecretError("invalid_response") from None

    async def test(self, config: Kubeconfig) -> ConnectionResult:
        """Verify authenticated identity before reporting a discovered server version.

        The SelfSubjectReview does not create a persistent workload. Public version
        discovery alone does not prove that the supplied credentials were accepted.
        Redirects and environment proxies are disabled; only the configured TLS
        endpoint receives this credential. Cancellation propagates through cleanup.
        """
        headers = (
            {"authorization": "Bearer " + config.token.get_secret_value()}
            if config.token is not None
            else {}
        )
        headers["accept-encoding"] = "identity"
        try:
            with anyio.fail_after(self.timeout):
                async with httpx2.AsyncClient(
                    base_url=config.server,
                    verify=config.tls_context(),
                    headers=headers,
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    review = await self._json(
                        client,
                        "POST",
                        "/apis/authentication.k8s.io/v1/selfsubjectreviews",
                        {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"},
                    )
                    try:
                        username = mapping(mapping(review.get("status")).get("userInfo")).get(
                            "username"
                        )
                    except SecretError:
                        raise SecretError("invalid_response") from None
                    if (
                        not isinstance(username, str)
                        or not username
                        or username == "system:anonymous"
                    ):
                        raise SecretError("unauthorized")
                    discovered = await self._json(client, "GET", "/version")
                    version = discovered.get("gitVersion")
                    if (
                        not isinstance(version, str)
                        or re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version)
                        is None
                        or len(version) > 64
                    ):
                        raise SecretError("invalid_response")
                    return ConnectionResult(True, "connected", version)
        except SecretError as exc:
            return ConnectionResult(False, exc.code)
        except (TimeoutError, httpx2.TimeoutException):
            return ConnectionResult(False, "timeout")
        except (httpx2.HTTPError, httpx2.InvalidURL, ssl.SSLError, OSError):
            return ConnectionResult(False, "unreachable")
