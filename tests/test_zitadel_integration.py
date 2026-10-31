"""Exercise production OIDC exchange and core sessions against disposable real Zitadel.

The fixture uses the upstream Session/CreateCallback APIs in place of driving its
browser login UI. It still obtains genuine one-use codes and signed ID tokens.
A generated trusted test CA terminates TLS; certificate verification stays enabled.
"""

import asyncio
import http.client
import ipaddress
import json
import os
import secrets
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx2 as httpx
import pytest
from conftest import POSTGRES
from cryptography.fernet import Fernet
from pydantic import SecretStr
from test_auth import auth_settings as auth_settings
from tinypods_core.auth import AuthError, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.federation import BrowserLogin
from tinypods_core.auth.providers.zitadel.oidc import ZitadelOIDC
from tinypods_core.auth.providers.zitadel.sessions import ZitadelSessions
from tinypods_core.auth.tokens import digest
from tinypods_core.db import Database
from tinypods_core.db.models import AuthSession, LocalCredential
from tinypods_core.rate_limit import RateLimiter
from tinypods_core.settings import Settings

ZITADEL = (
    "ghcr.io/zitadel/zitadel@sha256:"
    "15543f7a80c3e37905fe8c7db56af7ed2a9ece256704322c1449d7e8453afdab"
)
CALLBACK = "https://tinypods.example/auth/providers/zitadel/callback"


def docker(*arguments: str) -> str:
    """Run bounded commands against only fixture-owned names, without printing secrets."""
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=180, check=True
    )
    return result.stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Expose the authorize redirect without invoking a real browser login interface."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        stream: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        """Leave redirect handling to the fixture's checked-session ceremony."""
        return None


@dataclass(frozen=True)
class ZitadelFixture:
    """Own test-only upstream credentials and verified TLS connections."""

    issuer: str
    tls: ssl.SSLContext
    admin_pat: str = field(repr=False)
    login_pat: str = field(repr=False)
    project_id: str
    user_id: str
    password: str = field(repr=False)
    network: str = ""
    directory: Path | None = None
    sessions: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def request(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        login: bool = False,
        method: str | None = None,
    ) -> dict[str, Any]:
        """Call fixture administration; redact upstream bodies on failure."""
        token = self.login_pat if login else self.admin_pat
        request = urllib.request.Request(
            self.issuer + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self.tls, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError(f"Disposable Zitadel {path} returned {error.code}") from None

    def application(
        self, method: str, *, refresh: bool = False, backchannel_uri: str = ""
    ) -> dict[str, Any]:
        """Register an upstream Web client using exact production HTTPS redirects."""
        return self.request(
            "/management/v1/projects/" + self.project_id + "/apps/oidc",
            {
                "name": "TinyPods integration " + method + secrets.token_hex(4),
                "redirectUris": [CALLBACK],
                "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
                "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
                + (["OIDC_GRANT_TYPE_REFRESH_TOKEN"] if refresh else []),
                "appType": "OIDC_APP_TYPE_WEB",
                "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC"
                if method == "client_secret_basic"
                else "OIDC_AUTH_METHOD_TYPE_NONE",
                "postLogoutRedirectUris": ["https://tinypods.example/"],
                "devMode": bool(backchannel_uri),
                "backChannelLogoutUri": backchannel_uri,
            },
        )

    def approve(self, authorization_url: str) -> dict[str, list[str]]:
        """Authenticate a real test user and complete a real V2 authorization request."""
        expected = urllib.parse.urlsplit(self.issuer)
        target = urllib.parse.urlsplit(authorization_url)
        assert (target.scheme, target.netloc) == (expected.scheme, expected.netloc)
        query = urllib.parse.parse_qs(target.query)
        assert query["code_challenge_method"] == ["S256"]
        assert "code_verifier" not in query and "client_secret" not in query
        opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=self.tls)
        )
        try:
            opener.open(authorization_url, timeout=10)
        except urllib.error.HTTPError as error:
            assert error.code in (302, 303)
            location = error.headers["Location"]
        else:
            raise AssertionError("Zitadel authorization did not request authentication")
        auth_request = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["authRequest"][
            0
        ]
        assert auth_request.startswith("V2_")
        checked = self.request(
            "/v2/sessions",
            {
                "checks": {
                    "user": {"userId": self.user_id},
                    "password": {"password": self.password},
                },
                "lifetime": "600s",
            },
        )
        self.sessions.append((checked["sessionId"], checked["sessionToken"]))
        completed = self.request(
            "/v2/oidc/auth_requests/" + auth_request,
            {
                "session": {
                    "sessionId": checked["sessionId"],
                    "sessionToken": checked["sessionToken"],
                },
            },
            login=True,
        )
        response = urllib.parse.parse_qs(urllib.parse.urlsplit(completed["callbackUrl"]).query)
        assert "code" in response and "error" not in response
        assert response["state"] == query["state"]
        return response


@pytest.fixture(scope="module")
def real_zitadel(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ZitadelFixture]:
    """Start isolated PostgreSQL, actual Zitadel and a verified HTTPS test front proxy."""
    name = "tinypods-zitadel-" + secrets.token_hex(6)
    directory = tmp_path_factory.mktemp(name)
    cert, key = directory / "ca.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        raw_port = reserved.getsockname()[1]

    class Proxy(BaseHTTPRequestHandler):
        """Terminate only fixture TLS and forward REST/OIDC traffic to fixture loopback."""

        def log_message(self, format: str, *args: object) -> None:
            """Avoid recording query codes or credentials in test-server access logs."""

        def proxy(self) -> None:
            """Forward a bounded request and fully drain the upstream response."""
            connection = http.client.HTTPConnection("127.0.0.1", raw_port, timeout=10)
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = dict(self.headers)
                headers["X-Forwarded-Proto"] = "https"
                connection.request(self.command, self.path, body=body or None, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                self.send_response(response.status)
                for header, value in response.getheaders():
                    if header.lower() not in {"transfer-encoding", "connection", "content-length"}:
                        self.send_header(header, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, http.client.HTTPException):
                self.send_error(503)
            finally:
                connection.close()

        do_GET = proxy
        do_POST = proxy
        do_DELETE = proxy

    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    server.daemon_threads = True
    try:
        server.socket = server_tls.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except BaseException:
        # Setup has not yielded yet, so pytest cannot run fixture teardown for us.
        server.server_close()
        raise
    port = server.server_address[1]
    issuer = f"https://localhost:{port}"
    tls = ssl.create_default_context(cafile=str(cert))
    password = secrets.token_urlsafe(24) + "Aa1!"
    containers = [name + "-pg", name + "-idp"]
    try:
        docker("network", "create", name)
        docker(
            "run",
            "-d",
            "--name",
            containers[0],
            "--network",
            name,
            "-e",
            "POSTGRES_PASSWORD=" + password,
            "-e",
            "POSTGRES_DB=zitadel",
            POSTGRES,
        )
        deadline = time.monotonic() + 45
        while subprocess.run(
            ["docker", "exec", containers[0], "pg_isready", "-U", "postgres"],
            capture_output=True,
            timeout=10,
        ).returncode:
            assert time.monotonic() < deadline, "Disposable Zitadel database unavailable"
            time.sleep(0.1)
        subnet = ipaddress.ip_network(
            json.loads(docker("network", "inspect", name))[0]["IPAM"]["Config"][0]["Subnet"]
        )
        private = ipaddress.ip_network("172.16.0.0/12")
        assert isinstance(subnet, ipaddress.IPv4Network) and subnet.subnet_of(private)
        # The only private destination exception is this disposable test network.
        # Keep loopback, metadata endpoints and every other private range blocked.
        denylist = [
            "localhost",
            "0.0.0.0/8",
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "192.168.0.0/16",
            "198.18.0.0/15",
            "::/128",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ]
        denylist.extend(str(network) for network in private.address_exclude(subnet))
        environment = {
            "ZITADEL_HTTPCLIENT_DENYLIST": ",".join(denylist),
            "ZITADEL_PORT": "8080",
            "ZITADEL_EXTERNALDOMAIN": "localhost",
            "ZITADEL_EXTERNALPORT": str(port),
            "ZITADEL_EXTERNALSECURE": "true",
            "ZITADEL_TLS_ENABLED": "false",
            "ZITADEL_DATABASE_POSTGRES_DSN": f"postgresql://postgres:{password}@{containers[0]}:5432/zitadel?sslmode=disable",
            "ZITADEL_FIRSTINSTANCE_PATPATH": "/probe/admin.pat",
            "ZITADEL_FIRSTINSTANCE_LOGINCLIENTPATPATH": "/probe/login.pat",
            "ZITADEL_FIRSTINSTANCE_ORG_LOGINCLIENT_MACHINE_USERNAME": "login-client",
            "ZITADEL_FIRSTINSTANCE_ORG_LOGINCLIENT_MACHINE_NAME": "Disposable login client",
            "ZITADEL_FIRSTINSTANCE_ORG_LOGINCLIENT_PAT_EXPIRATIONDATE": "2099-01-01T00:00:00Z",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_USERNAME": "probe-admin",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_NAME": "Disposable test admin",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_PAT_EXPIRATIONDATE": "2099-01-01T00:00:00Z",
            "ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD": password,
            "ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORDCHANGEREQUIRED": "false",
            "ZITADEL_DEFAULTINSTANCE_FEATURES_LOGINV2_REQUIRED": "true",
            "ZITADEL_DEFAULTINSTANCE_FEATURES_LOGINV2_BASEURI": issuer + "/ui/v2/login/",
            "ZITADEL_OIDC_DEFAULTLOGINURLV2": issuer + "/ui/v2/login/login?authRequest=",
        }
        arguments = [
            "run",
            "-d",
            "--name",
            containers[1],
            "--network",
            name,
            "-p",
            f"127.0.0.1:{raw_port}:8080",
            "-v",
            f"{directory}:/probe",
            "--user",
            "0",
        ]
        for setting, value in environment.items():
            arguments += ["-e", setting + "=" + value]
        arguments += [
            ZITADEL,
            "start-from-init",
            "--masterkey",
            secrets.token_hex(16),
            "--tlsMode",
            "external",
        ]
        docker(*arguments)
        pat_path = directory / "admin.pat"
        deadline = time.monotonic() + 150
        while not pat_path.exists():
            assert time.monotonic() < deadline, "Disposable Zitadel bootstrap unavailable"
            time.sleep(0.25)
        base = ZitadelFixture(
            issuer,
            tls,
            pat_path.read_text().strip(),
            (directory / "login.pat").read_text().strip(),
            "",
            "",
            password,
        )
        # Upstream's REST-to-gRPC gateway can still be starting after metadata/ready work.
        deadline = time.monotonic() + 45
        while True:
            try:
                project = base.request(
                    "/management/v1/projects", {"name": "TinyPods disposable integration"}
                )
                break
            except (AssertionError, OSError):
                assert time.monotonic() < deadline, "Disposable Zitadel management API unavailable"
                time.sleep(0.25)
        user = base.request(
            "/v2/users/human",
            {
                "username": "integration-user",
                "profile": {"givenName": "Integration", "familyName": "User"},
                "email": {"email": "integration@example.test", "isVerified": True},
                "password": {"password": password, "changeRequired": False},
            },
        )
        yield ZitadelFixture(
            issuer,
            tls,
            base.admin_pat,
            base.login_pat,
            project["id"],
            user["userId"],
            password,
            name,
            directory,
        )
    finally:
        cleanup_failures: list[str] = []
        # Attempt every generated resource independently, even after daemon/timeout failures.
        for container in reversed(containers):
            try:
                result = subprocess.run(
                    ["docker", "rm", "-f", container], capture_output=True, text=True, timeout=30
                )
                if result.returncode and "No such container" not in result.stderr:
                    cleanup_failures.append(container)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failures.append(container)
        try:
            result = subprocess.run(
                ["docker", "network", "rm", name], capture_output=True, text=True, timeout=30
            )
            if result.returncode and "not found" not in result.stderr:
                cleanup_failures.append(name)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failures.append(name)
        try:
            server.shutdown()
        finally:
            try:
                server.server_close()
            finally:
                thread.join(timeout=10)
        if thread.is_alive():
            cleanup_failures.append("TLS listener thread")
        if cleanup_failures:
            pytest.fail("Disposable resource cleanup failed: " + ", ".join(cleanup_failures))


@pytest.mark.parametrize("method", ["none", "client_secret_basic"])
def test_real_zitadel_tls_pkce_browser_session_and_authorization(
    real_zitadel: ZitadelFixture,
    auth_settings: Settings,
    method: str,
) -> None:
    """Validate actual signed upstream tokens through production adapter and core policy."""
    app = real_zitadel.application(method)
    settings = auth_settings.model_copy(
        update={
            "zitadel_enabled": True,
            "zitadel_issuer": real_zitadel.issuer,
            "zitadel_client_id": app["clientId"],
            "zitadel_redirect_uri": CALLBACK,
            "zitadel_token_auth_method": method,
            "zitadel_client_secret": SecretStr(app["clientSecret"])
            if method == "client_secret_basic"
            else None,
            "zitadel_post_logout_uri": "https://tinypods.example/",
        }
    )

    async def scenario() -> None:
        database = Database(settings)
        limiter = RateLimiter(settings)
        adapter = ZitadelOIDC(settings, transport=httpx.AsyncHTTPTransport(verify=real_zitadel.tls))
        browser = BrowserLogin(database, settings, adapter, limiter)
        try:
            start = await browser.start("/applications")
            response = await asyncio.to_thread(real_zitadel.approve, start.authorization_url)
            login, return_to = await browser.finish(
                response["state"][0],
                start.browser_token,
                response["code"][0],
                response_issuer=response.get("iss", [None])[0],
            )
            assert return_to == "/applications" and not login.principal.is_owner
            principal = await browser.sessions.authenticate(
                login.token, csrf_token=login.csrf_token
            )
            policy = AuthorizationService(browser.sessions)
            assert (
                await policy.require_access(principal, Scope.APP, owner_id=principal.user_id)
                == principal
            )
            with pytest.raises(AuthError) as error:
                await policy.require_access(principal, Scope.CATALOG_ADMIN)
            assert error.value.status == 403
            async with database.session() as session:
                assert await session.get(LocalCredential, principal.user_id) is None
                record = await session.get(AuthSession, digest(login.token))
                assert record is not None and record.source_provider == "zitadel"
                assert record.external_issuer == real_zitadel.issuer
                assert record.external_subject == real_zitadel.user_id
            with pytest.raises(AuthError) as error:
                await browser.finish(response["state"][0], start.browser_token, response["code"][0])
            assert error.value.status == 401
            logout = await adapter.logout_url()
            assert logout is not None and "id_token" not in logout and "client_secret" not in logout
            await browser.sessions.logout(principal)
            with pytest.raises(AuthError) as error:
                await browser.sessions.authenticate(login.token)
            assert error.value.status == 401
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("attack", ["pkce", "nonce", "code_replay"])
def test_real_zitadel_rejects_bad_proof_and_replayed_codes(
    real_zitadel: ZitadelFixture,
    attack: str,
) -> None:
    """Exercise genuine upstream PKCE/replay rejection and signed ID-token nonce validation."""
    application = real_zitadel.application("none")
    settings = Settings(
        zitadel_enabled=True,
        zitadel_issuer=real_zitadel.issuer,
        zitadel_client_id=application["clientId"],
        zitadel_redirect_uri=CALLBACK,
    )

    async def scenario() -> None:
        adapter = ZitadelOIDC(settings, transport=httpx.AsyncHTTPTransport(verify=real_zitadel.tls))
        state, nonce, verifier = (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
        )
        url = await adapter.authorization_url(state, nonce, verifier)
        response = await asyncio.to_thread(real_zitadel.approve, url)
        code = response["code"][0]
        if attack == "code_replay":
            verified = await adapter.exchange(code, nonce, verifier)
            assert verified.subject == real_zitadel.user_id
        with pytest.raises(AuthError) as error:
            await adapter.exchange(
                code,
                secrets.token_urlsafe(32) if attack == "nonce" else nonce,
                secrets.token_urlsafe(48) if attack == "pkce" else verifier,
            )
        assert error.value.status == 401

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["none", "client_secret_basic"])
def test_real_zitadel_refresh_rotation_and_family_revocation(
    real_zitadel: ZitadelFixture, auth_settings: Settings, method: str
) -> None:
    """Exchange real provider refresh tokens over verified TLS and rotate browser credentials."""
    application = real_zitadel.application(method, refresh=True)
    settings = Settings.model_validate(
        {
            **auth_settings.model_dump(),
            "zitadel_enabled": True,
            "zitadel_issuer": real_zitadel.issuer,
            "zitadel_client_id": application["clientId"],
            "zitadel_redirect_uri": CALLBACK,
            "zitadel_token_auth_method": method,
            "zitadel_client_secret": SecretStr(application["clientSecret"])
            if method == "client_secret_basic"
            else None,
            "zitadel_refresh_enabled": True,
            "zitadel_refresh_encryption_keys": (SecretStr(Fernet.generate_key().decode()),),
        }
    )

    async def scenario() -> None:
        database, limiter = Database(settings), RateLimiter(settings)
        adapter = ZitadelOIDC(settings, transport=httpx.AsyncHTTPTransport(verify=real_zitadel.tls))
        browser = BrowserLogin(database, settings, adapter, limiter)
        lifecycle = ZitadelSessions(
            database, settings, adapter, browser.sessions, browser.configuration_digest, limiter
        )
        browser.lifecycle = lifecycle
        try:
            start = await browser.start()
            response = await asyncio.to_thread(real_zitadel.approve, start.authorization_url)
            login, _ = await browser.finish(
                response["state"][0], start.browser_token, response["code"][0]
            )
            assert login.refresh_token is not None and login.refresh_expires_at is not None
            renewed = await lifecycle.refresh(login.refresh_token, login.csrf_token)
            assert (
                renewed.refresh_token is not None and renewed.refresh_token != login.refresh_token
            )
            assert (
                renewed.token != login.token
                and renewed.refresh_expires_at == login.refresh_expires_at
            )
            assert await browser.sessions.authenticate(renewed.token) == renewed.principal
            with pytest.raises(AuthError):
                await browser.sessions.authenticate(login.token)
            await browser.sessions.logout(renewed.principal)
            with pytest.raises(AuthError):
                await lifecycle.refresh(renewed.refresh_token, renewed.csrf_token)
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())


@pytest.fixture
def real_logout_receiver(real_zitadel: ZitadelFixture) -> Iterator[tuple[str, Path]]:
    """Receive genuine logout notifications on the fixture's private Docker network only."""
    assert real_zitadel.directory is not None and real_zitadel.network
    name = "tinypods-logout-" + secrets.token_hex(6)
    script = real_zitadel.directory / (name + ".py")
    sink = real_zitadel.directory / (name + ".jwt")
    script.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "from pathlib import Path\nfrom urllib.parse import parse_qs\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def log_message(self, *args): pass\n"
        " def do_GET(self):\n  self.send_response(200); self.end_headers()\n"
        " def do_POST(self):\n"
        "  length=int(self.headers.get('Content-Length','0'))\n"
        "  if length > 65536: self.send_error(400); return\n"
        "  value=parse_qs(self.rfile.read(length).decode())['logout_token'][0]\n"
        + "  path=Path("
        + repr(str(sink))
        + ")\n"
        + "  path=Path('/probe')/path.name\n  path.write_text(value); path.chmod(0o600)\n"
        "  self.send_response(200); self.end_headers()\n"
        "HTTPServer(('0.0.0.0',8080), Handler).serve_forever()\n"
    )
    image = (
        "python:3.13-slim-bookworm@sha256:"
        "281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f"
    )
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            real_zitadel.network,
            "--user",
            str(os.getuid()) + ":" + str(os.getgid()),
            "-v",
            str(real_zitadel.directory) + ":/probe",
            image,
            "python",
            "/probe/" + script.name,
        )
        deadline = time.monotonic() + 20
        while subprocess.run(
            [
                "docker",
                "exec",
                name,
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://localhost:8080')",
            ],
            capture_output=True,
            timeout=10,
        ).returncode:
            assert time.monotonic() < deadline, "Disposable logout receiver unavailable"
            time.sleep(0.1)
        yield "http://" + name + ":8080/logout", sink
    finally:
        result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        assert result.returncode == 0, "Disposable logout receiver cleanup failed"


def test_real_zitadel_backchannel_notification_revokes_session_and_refresh(
    real_zitadel: ZitadelFixture, real_logout_receiver: tuple[str, Path], auth_settings: Settings
) -> None:
    """Validate a genuine provider-generated logout token and revoke its matching local family."""
    url, sink = real_logout_receiver
    application = real_zitadel.application("client_secret_basic", refresh=True, backchannel_uri=url)
    settings = Settings.model_validate(
        {
            **auth_settings.model_dump(),
            "zitadel_enabled": True,
            "zitadel_issuer": real_zitadel.issuer,
            "zitadel_client_id": application["clientId"],
            "zitadel_redirect_uri": CALLBACK,
            "zitadel_token_auth_method": "client_secret_basic",
            "zitadel_client_secret": SecretStr(application["clientSecret"]),
            "zitadel_refresh_enabled": True,
            "zitadel_refresh_encryption_keys": (SecretStr(Fernet.generate_key().decode()),),
        }
    )

    async def scenario() -> None:
        database, limiter = Database(settings), RateLimiter(settings)
        adapter = ZitadelOIDC(settings, transport=httpx.AsyncHTTPTransport(verify=real_zitadel.tls))
        browser = BrowserLogin(database, settings, adapter, limiter)
        lifecycle = ZitadelSessions(
            database, settings, adapter, browser.sessions, browser.configuration_digest, limiter
        )
        browser.lifecycle = lifecycle
        try:
            start = await browser.start()
            response = await asyncio.to_thread(real_zitadel.approve, start.authorization_url)
            login, _ = await browser.finish(
                response["state"][0], start.browser_token, response["code"][0]
            )
            assert login.refresh_token is not None
            session_id, session_token = real_zitadel.sessions[-1]
            await asyncio.to_thread(
                real_zitadel.request,
                "/v2/sessions/" + session_id,
                {"sessionToken": session_token},
                method="DELETE",
            )
            deadline = time.monotonic() + 20
            while not sink.exists():
                assert time.monotonic() < deadline, "Real provider logout notification missing"
                await asyncio.sleep(0.1)
            encoded = sink.read_text()
            notice = await adapter.validate_logout_token(encoded)
            assert notice.subject == real_zitadel.user_id and notice.session_id is not None
            await lifecycle.backchannel_logout(encoded)
            with pytest.raises(AuthError):
                await browser.sessions.authenticate(login.token)
            with pytest.raises(AuthError):
                await lifecycle.refresh(login.refresh_token, login.csrf_token)
            await lifecycle.backchannel_logout(encoded)
        finally:
            await limiter.close()
            await database.close()

    asyncio.run(scenario())
