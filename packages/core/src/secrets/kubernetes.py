"""Bounded kubeconfig parsing and native async Kubernetes identity verification."""

from __future__ import annotations

import base64
import binascii
import os
import re
import ssl
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlsplit

import aiohttp
import anyio
import yaml  # type: ignore[import-untyped]
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from kubernetes.aio.client import (
    ApiClient,
    AuthenticationV1Api,
    Configuration,
    V1SelfSubjectReview,
    VersionApi,
    VersionInfo,
)
from kubernetes.aio.client.exceptions import ApiException
from pydantic import BaseModel, SecretBytes, SecretStr
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
            # Kubernetes proxies may publish a cluster beneath an HTTPS base path.
            # Preserve that prefix; the SDK appends its API routes to the host.
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
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


async def reject_redirect(
    session: aiohttp.ClientSession,
    context: SimpleNamespace,
    params: aiohttp.TraceRequestRedirectParams,
) -> None:
    """Stop redirects before a second request can receive a cluster credential."""
    raise SecretError("invalid_response")


def build_sdk_client(config: Kubeconfig) -> ApiClient:
    """Build TLS in the filesystem worker; SDK network sessions are created lazily.

    No SDK kubeconfig loader is used: untrusted documents cannot run plugins or
    load arbitrary host files. The existing validated embedded TLS material owns
    its private temporary-file lifecycle and is removed before any network I/O.
    """
    context = config.tls_context()
    configuration = Configuration(
        host=config.server,
        verify_ssl=True,
        proxy=None,
        api_key={"BearerToken": config.token.get_secret_value()}
        if config.token is not None
        else {},
        api_key_prefix={"BearerToken": "Bearer"},
        client_session_kwargs={"trust_env": False, "auto_decompress": False, "read_bufsize": 16384},
    )
    configuration.client_go_retries = False
    configuration.debug = False
    client = ApiClient(configuration)
    # The SDK's lazy connector factory uses this owned, verified SSLContext.
    # Installing it before the first request preserves embedded mTLS support.
    client.rest_client.ssl_context = context
    client.default_headers["Accept-Encoding"] = "identity"
    return client


class KubernetesClient:
    """Use the official native async SDK with bounded replies and owned cleanup."""

    def __init__(self, *, timeout: float = 10) -> None:
        """Retain one bounded deadline for identity review and version discovery."""
        self.timeout = timeout

    async def _model[DTO: BaseModel](
        self, response: aiohttp.ClientResponse, model: type[DTO]
    ) -> DTO:
        """Read bounded bytes before using official SDK response model validation."""
        try:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise SecretError("invalid_response")
            if response.status in (401, 403):
                raise SecretError("unauthorized" if response.status == 401 else "forbidden")
            if response.status not in (200, 201):
                raise SecretError("invalid_response")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                payload.extend(chunk)
                if len(payload) > 65536:
                    raise SecretError("invalid_response")
            try:
                return model.model_validate_json(payload)
            except (ValueError, UnicodeError, RecursionError):
                raise SecretError("invalid_response") from None
        finally:
            response.close()

    async def test(self, config: Kubeconfig) -> ConnectionResult:
        """Await official SDK APIs; only bounded TLS/file setup runs in a worker.

        Cancellation propagates through SDK session cleanup. Identity review is
        required before version discovery; public discovery alone is insufficient.
        SDK retries, environment proxies, decompression and redirects are disabled.
        """
        try:
            with anyio.fail_after(self.timeout):
                client = await anyio.to_thread.run_sync(build_sdk_client, config)
                trace = aiohttp.TraceConfig()
                trace.on_request_redirect.append(reject_redirect)
                client.configuration.trace_configs = [trace]
                async with client:
                    response = await AuthenticationV1Api(
                        client
                    ).create_self_subject_review_without_preload_content(
                        V1SelfSubjectReview(
                            api_version="authentication.k8s.io/v1", kind="SelfSubjectReview"
                        ),
                        _request_timeout=float(self.timeout),
                    )
                    review = await self._model(response, V1SelfSubjectReview)
                    if review.status is None or review.status.user_info is None:
                        raise SecretError("invalid_response")
                    username = review.status.user_info.username
                    if not username or username == "system:anonymous":
                        raise SecretError("unauthorized")
                    response = await VersionApi(client).get_code_without_preload_content(
                        _request_timeout=float(self.timeout)
                    )
                    discovered = await self._model(response, VersionInfo)
                    version = discovered.git_version
                    if (
                        re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version)
                        is None
                        or len(version) > 64
                    ):
                        raise SecretError("invalid_response")
                    return ConnectionResult(True, "connected", version)
        except SecretError as exc:
            return ConnectionResult(False, exc.code)
        except TimeoutError:
            return ConnectionResult(False, "timeout")
        except (aiohttp.ClientError, ApiException, ssl.SSLError, OSError, ValueError):
            return ConnectionResult(False, "unreachable")
