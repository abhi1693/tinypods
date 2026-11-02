"""Real TLS Kubernetes exchanges, bounded diagnostics and cancellation ownership."""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

import aiohttp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from kubernetes.aio.client import AuthenticationV1Api
from pydantic import SecretBytes, SecretStr
from secret_fixtures import TOKEN, document
from tinypods_core.secrets.crypto import SecretError
from tinypods_core.secrets.kubernetes import Kubeconfig, KubernetesClient


@dataclass(frozen=True)
class Certificates:
    """Own only freshly generated disposable TLS fixture material."""

    authority: bytes
    server_certificate: Path
    server_key: Path
    client_certificate: bytes
    client_key: bytes


@pytest.fixture
def certificates(tmp_path: Path) -> Certificates:
    """Create a strict-verification-compatible CA and server/client leaf certificates."""
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable TLS CA")])

    def signed(
        name: str, key: ec.EllipticCurvePrivateKey, purpose: x509.ObjectIdentifier | None = None
    ) -> x509.Certificate:
        """Sign fixture certificates with constrained usage and complete trust identifiers."""
        subject = (
            root_name
            if purpose is None
            else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        )
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(root_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=purpose is None, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=purpose is None,
                    crl_sign=purpose is None,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
        )
        if purpose is not None:
            builder = builder.add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
        if purpose == ExtendedKeyUsageOID.SERVER_AUTH:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ip_address("127.0.0.1")), x509.DNSName("localhost")]
                ),
                critical=False,
            )
        return builder.sign(root_key, hashes.SHA256())

    authority = signed("CA", root_key).public_bytes(serialization.Encoding.PEM)
    server_key = ec.generate_private_key(ec.SECP256R1())
    client_key = ec.generate_private_key(ec.SECP256R1())
    server_certificate = tmp_path / "server.crt"
    server_private = tmp_path / "server.key"
    server_certificate.write_bytes(
        signed("localhost", server_key, ExtendedKeyUsageOID.SERVER_AUTH).public_bytes(
            serialization.Encoding.PEM
        )
    )
    server_private.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Certificates(
        authority,
        server_certificate,
        server_private,
        signed("test-client", client_key, ExtendedKeyUsageOID.CLIENT_AUTH).public_bytes(
            serialization.Encoding.PEM
        ),
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@dataclass(frozen=True)
class Reply:
    """Control only fixture replies, including malformed and oversized content."""

    status: int
    body: bytes
    encoding: str = "identity"


class TLSFixture:
    """Serve a tiny real TLS protocol peer and own all accepted connection tasks."""

    def __init__(
        self,
        certificates: Certificates,
        *,
        mutual: bool = False,
        override: Mapping[str, Reply] | None = None,
        gate: asyncio.Event | None = None,
        prefix: str = "",
    ) -> None:
        """Configure verified fixture certificates and optional deterministic response control."""
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificates.server_certificate, certificates.server_key)
        self.context.load_verify_locations(cadata=certificates.authority.decode())
        if mutual:
            self.context.verify_mode = ssl.CERT_REQUIRED
        self.mutual = mutual
        self.override = override or {}
        self.gate = gate
        self.prefix = prefix
        self.started = asyncio.Event()
        self.tasks: set[asyncio.Task[None]] = set()
        self.requests: list[tuple[str, str]] = []
        self.url = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Consume one bounded request and close the connection after a controlled reply."""
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            separator = bytes([13, 10])
            data = await reader.readuntil(separator + separator)
            lines = data.decode().split(separator.decode())
            method, path, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            headers = {key.lower(): value for key, value in headers.items()}
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.requests.append((method, path))
            assert path.startswith(self.prefix)
            path = path[len(self.prefix) :]
            self.started.set()
            assert headers.get("accept-encoding") == "identity"
            if self.gate is not None:
                await self.gate.wait()
            authenticated = (
                bool(writer.get_extra_info("peercert"))
                if self.mutual
                else headers.get("authorization") == "Bearer " + TOKEN
            )
            if not authenticated:
                reply = Reply(401, b'{"message":"fixture-private-error"}')
            elif path.rstrip("/") in self.override:
                reply = self.override[path.rstrip("/")]
            elif path == "/apis/authentication.k8s.io/v1/selfsubjectreviews":
                assert method == "POST" and json.loads(body)["kind"] == "SelfSubjectReview"
                reply = Reply(201, b'{"status":{"userInfo":{"username":"fixture-client"}}}')
            else:
                assert method == "GET" and path == "/version/"
                reply = Reply(
                    200,
                    json.dumps(
                        {
                            "major": "1",
                            "minor": "33",
                            "gitVersion": "v1.33.1",
                            "gitCommit": "fixture",
                            "gitTreeState": "clean",
                            "buildDate": "2026-01-01T00:00:00Z",
                            "goVersion": "go1.24",
                            "compiler": "gc",
                            "platform": "linux/amd64",
                        }
                    ).encode(),
                )
            response_headers = [
                f"HTTP/1.1 {reply.status} Fixture",
                "Content-Type: application/json",
                f"Content-Encoding: {reply.encoding}",
                f"Content-Length: {len(reply.body)}",
                "Connection: close",
                "",
                "",
            ]
            writer.write(
                separator.join(header.encode() for header in response_headers) + reply.body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[TLSFixture]:
        """Use an ephemeral loopback port and close only fixture-owned resources."""
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0, ssl=self.context)
        self.url = "https://127.0.0.1:" + str(server.sockets[0].getsockname()[1]) + self.prefix
        try:
            yield self
        finally:
            server.close()
            await server.wait_closed()
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def config_for(fixture: TLSFixture, certificates: Certificates, mutual: bool = False) -> Kubeconfig:
    """Parse an ordinary self-contained kubeconfig for a verified real TLS endpoint."""
    value = document(fixture.url)
    value["clusters"][0]["cluster"]["certificate-authority-data"] = base64.b64encode(
        certificates.authority
    ).decode()
    if mutual:
        value["users"][0]["user"] = {
            "client-certificate-data": base64.b64encode(certificates.client_certificate).decode(),
            "client-key-data": base64.b64encode(certificates.client_key).decode(),
        }
    return Kubeconfig.parse(json.dumps(value).encode())


@pytest.mark.parametrize("mutual", [False, True])
def test_real_tls_token_and_client_certificate(
    certificates: Certificates, mutual: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify real server trust, selected credentials, identity review and version discovery."""

    async def scenario() -> None:
        async with TLSFixture(certificates, mutual=mutual).running() as peer:
            result = await KubernetesClient().test(config_for(peer, certificates, mutual))
            assert (
                result.connected
                and result.reason == "connected"
                and result.kubernetes_version == "v1.33.1"
            )
            assert peer.requests == [
                ("POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews"),
                ("GET", "/version/"),
            ]

    asyncio.run(scenario())
    assert TOKEN not in caplog.text and "PRIVATE KEY" not in caplog.text


@pytest.mark.parametrize(
    "reply,reason",
    [
        (Reply(401, b"private token"), "unauthorized"),
        (Reply(403, b"private token"), "forbidden"),
        (Reply(302, b"redirect"), "invalid_response"),
        (Reply(500, b"private token"), "invalid_response"),
        (Reply(200, b"not JSON"), "invalid_response"),
        (Reply(200, b"[]"), "invalid_response"),
        (Reply(200, b"{}"), "invalid_response"),
        (Reply(200, b'{"status":{"userInfo":{"username":"system:anonymous"}}}'), "unauthorized"),
        (Reply(200, b'{"status":{"userInfo":{"username":7}}}'), "invalid_response"),
        (Reply(200, b'{"status":{"userInfo":{"username":""}}}'), "unauthorized"),
        (Reply(200, b"x" * 65537), "invalid_response"),
        (Reply(200, b"{}", "gzip"), "invalid_response"),
    ],
)
def test_identity_review_diagnostics_are_bounded(
    certificates: Certificates, reply: Reply, reason: str
) -> None:
    """Never expose upstream bodies or accept anonymous discovery as authentication."""

    async def scenario() -> None:
        async with TLSFixture(
            certificates, override={"/apis/authentication.k8s.io/v1/selfsubjectreviews": reply}
        ).running() as peer:
            result = await KubernetesClient().test(config_for(peer, certificates))
            assert (
                not result.connected
                and result.reason == reason
                and result.kubernetes_version is None
            )
            assert "private token" not in repr(result)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"gitVersion":true}',
        *[
            json.dumps(
                {
                    "major": "1",
                    "minor": "33",
                    "gitVersion": version,
                    "gitCommit": "fixture",
                    "gitTreeState": "clean",
                    "buildDate": "2026-01-01T00:00:00Z",
                    "goVersion": "go1.24",
                    "compiler": "gc",
                    "platform": "linux/amd64",
                }
            ).encode()
            for version in ("<unexpected>", "v" + "1" * 65 + ".2.3")
        ],
    ],
)
def test_version_is_a_constrained_public_value(certificates: Certificates, body: bytes) -> None:
    """Return a typed version string rather than arbitrary server-provided text."""

    async def scenario() -> None:
        async with TLSFixture(
            certificates, override={"/version": Reply(200, body)}
        ).running() as peer:
            result = await KubernetesClient().test(config_for(peer, certificates))
            assert not result.connected and result.reason == "invalid_response"

    asyncio.run(scenario())


def test_tls_failure_timeout_and_cancellation(certificates: Certificates) -> None:
    """Untrusted TLS sends no bearer credential, and pending request cleanup stays bounded."""

    async def scenario() -> None:
        async with TLSFixture(certificates).running() as peer:
            untrusted = Kubeconfig(peer.url, token=SecretStr(TOKEN))
            result = await KubernetesClient().test(untrusted)
            assert result.reason == "unreachable" and not peer.requests
        async with TLSFixture(certificates, gate=asyncio.Event()).running() as peer:
            result = await KubernetesClient(timeout=0.05).test(config_for(peer, certificates))
            assert result.reason == "timeout"
        async with TLSFixture(certificates, gate=asyncio.Event()).running() as peer:
            task = asyncio.create_task(KubernetesClient().test(config_for(peer, certificates)))
            await asyncio.wait_for(peer.started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not peer.tasks

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error,reason",
    [
        (aiohttp.ClientConnectionError("fixture"), "unreachable"),
        (TimeoutError("fixture"), "timeout"),
        (OSError("fixture"), "unreachable"),
    ],
)
def test_transport_failures_have_fixed_reasons(
    error: Exception, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Library and OS failures never propagate their potentially confidential messages."""

    async def failing(
        self: AuthenticationV1Api, *args: object, **kwargs: object
    ) -> aiohttp.ClientResponse:
        """Inject only the official SDK request boundary, without external traffic."""
        raise error

    monkeypatch.setattr(
        AuthenticationV1Api, "create_self_subject_review_without_preload_content", failing
    )

    async def scenario() -> None:
        config = Kubeconfig("https://example.test", token=SecretStr(TOKEN))
        assert (await KubernetesClient().test(config)).reason == reason

    asyncio.run(scenario())


def test_encrypted_private_key_cannot_trigger_a_password_prompt(certificates: Certificates) -> None:
    """Reject protected key input before invoking OpenSSL's file-loading prompt boundary."""
    key = serialization.load_pem_private_key(certificates.client_key, password=None)
    protected = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"fixture-only-password"),
    )
    config = Kubeconfig(
        "https://example.test",
        certificate=SecretBytes(certificates.client_certificate),
        key=SecretBytes(protected),
    )
    with pytest.raises(SecretError, match="invalid_kubeconfig"):
        config.tls_context()

    async def scenario() -> None:
        assert (await KubernetesClient().test(config)).reason == "invalid_kubeconfig"

    asyncio.run(scenario())


def test_deep_response_json_is_a_fixed_diagnostic(certificates: Certificates) -> None:
    """A bounded but excessively nested upstream document cannot escape error translation."""

    async def scenario() -> None:
        reply = Reply(200, b"[" * 1500 + b"0" + b"]" * 1500)
        async with TLSFixture(
            certificates, override={"/apis/authentication.k8s.io/v1/selfsubjectreviews": reply}
        ).running() as peer:
            result = await KubernetesClient().test(config_for(peer, certificates))
            assert result.reason == "invalid_response"

    asyncio.run(scenario())


@pytest.mark.parametrize("mutual", [False, True])
def test_sdk_preserves_cluster_proxy_prefix(certificates: Certificates, mutual: bool) -> None:
    """Identity and version calls stay beneath the selected proxy cluster path."""

    async def scenario() -> None:
        prefix = "/k8s/clusters/local"
        async with TLSFixture(certificates, mutual=mutual, prefix=prefix).running() as peer:
            result = await KubernetesClient().test(config_for(peer, certificates, mutual))
            assert result.connected
            assert peer.requests == [
                ("POST", prefix + "/apis/authentication.k8s.io/v1/selfsubjectreviews"),
                ("GET", prefix + "/version/"),
            ]

    asyncio.run(scenario())
