"""Controlled external-source tests: no remote servers, registry accounts or chart archives."""

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator

import httpx2 as httpx
import pytest
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.helm import parse_index
from tinypods_core.repositories.sources import (
    CHART_CONFIG,
    CHART_LAYER,
    SourceError,
    repository_url,
)
from tinypods_core.repositories.transport import MetadataHTTP, resolve_public_host
from tinypods_core.settings import RepositoryCredentials, Settings

DIGEST = "sha256:" + "a" * 64
INDEX = (
    "entries:\n  example:\n    - name: example\n      version: 1.0.0\n      digest: "
    + "a" * 64
    + "\n      description: Upstream chart description\n      icon: https://charts.example.test/icon.svg\n"
).encode()


async def public_dns(host: str) -> list[str]:
    """Resolve fixtures to a public address; mock transport prevents real connections."""
    return ["8.8.8.8"]


def client(handler: object, **settings: object) -> RepositoryClient:
    """Create the real client with only DNS/transport replaced by deterministic fixtures."""
    return RepositoryClient(
        MetadataHTTP(
            Settings(**settings), resolver=public_dns, transport=httpx.MockTransport(handler)
        )
    )


def test_helm_index_and_pinned_transport() -> None:
    """Use the source base URL, pin DNS, preserve Host/SNI and normalize index digests."""
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Assert the connection identity before returning ordinary public metadata."""
        seen.append(request)
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "charts.example.test"
        assert request.extensions["sni_hostname"] == "charts.example.test"
        assert request.url.path == "/charts/index.yaml"
        return httpx.Response(200, content=INDEX)

    result = asyncio.run(
        client(handler).fetch("helm_repository", "https://charts.example.test/charts/")
    )
    assert result.discovery_supported and len(result.versions) == 1
    assert result.versions[0].digest == DIGEST
    assert result.versions[0].description == "Upstream chart description"
    assert result.versions[0].icon == "https://charts.example.test/icon.svg"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "source,url",
    [
        ("helm_repository", "http://charts.example.test"),
        ("helm_repository", "https://user:pass@charts.example.test"),
        ("helm_repository", "https://charts.example.test/index.yaml"),
        ("helm_repository", "https://charts.example.test?token=secret"),
        ("helm_repository", "https://charts.example.test/#fragment"),
        ("helm_repository", "https://charts.example.test:8443"),
        ("helm_repository", "https://charts.example.test/../private"),
        ("oci_registry", "https://registry.example.test/team"),
        ("oci_registry", "oci://registry.example.test/team/%2e%2e"),
        ("helm_repository", "https://[broken"),
        ("helm_repository", "https://charts.example.test:bad"),
        ("helm_repository", "https://charts.example.test/\\bad"),
    ],
)
def test_invalid_source_bases(source: str, url: str) -> None:
    """Reject credentials, index-file inputs and ambiguous source locations before I/O."""
    with pytest.raises(ValueError):
        repository_url(source, url)
    with pytest.raises(SourceError, match="invalid_repository_url"):
        asyncio.run(client(lambda request: httpx.Response(200)).fetch(source, url))


@pytest.mark.parametrize(
    "body,maximum",
    [
        (b"entries: [", 10),
        (b"!!python/object/apply:os.system [echo]", 10),
        (b"entries: &a {example: *a}", 10),
        (b"entries: " + b"[" * 34 + b"]" * 34, 10),
        (b"\xff", 10),
        (b"entries: {}\nignored: " + b"[" + b"0," * 300 + b"0]", 1),
        (INDEX.replace(b"name: example", b"name: other"), 10),
        (INDEX.replace(b"version: 1.0.0", b"version: latest"), 10),
        (INDEX.replace(b"a" * 64, b"bad"), 10),
        (INDEX + INDEX.split(b"  example:")[1].replace(b"    -", b"    -", 1), 1),
    ],
)
def test_unsafe_or_invalid_index_documents(body: bytes, maximum: int) -> None:
    """Reject hostile graph structures, invalid identities and excess observations."""
    with pytest.raises(SourceError):
        parse_index(body, maximum)


def test_index_prefix_digest_empty_and_item_limit() -> None:
    """Accept canonical digests and empty sources, and bound complete source results."""
    assert parse_index(INDEX.replace(b"a" * 64, DIGEST.encode()), 10)[0].digest == DIGEST
    assert parse_index(b"entries: {}", 1) == []
    second = (
        INDEX + b"    - name: example\n      version: 2.0.0\n      digest: " + b"b" * 64 + b"\n"
    )
    with pytest.raises(SourceError, match="source_item_limit"):
        parse_index(second, 1)
    with pytest.raises(SourceError, match="source_unavailable"):
        asyncio.run(
            client(lambda request: httpx.Response(500)).fetch(
                "helm_repository", "https://charts.example.test"
            )
        )


@pytest.mark.parametrize(
    "url,addresses,hosts,code",
    [
        ("http://charts.example.test", ["8.8.8.8"], (), "source_not_allowed"),
        ("https://u:p@charts.example.test", ["8.8.8.8"], (), "source_not_allowed"),
        ("https://charts.example.test:8443", ["8.8.8.8"], (), "source_not_allowed"),
        ("https://charts.example.test#fragment", ["8.8.8.8"], (), "source_not_allowed"),
        ("https://charts.example.test/\\bad", ["8.8.8.8"], (), "source_not_allowed"),
        ("https://charts.example.test", ["8.8.8.8"], ("other.example.test",), "source_not_allowed"),
        ("https://charts.example.test", [], (), "source_not_public"),
        ("https://charts.example.test", ["127.0.0.1"], (), "source_not_public"),
        ("https://charts.example.test", ["8.8.8.8", "10.0.0.1"], (), "source_not_public"),
        ("https://charts.example.test", ["::1"], (), "source_not_public"),
        ("https://charts.example.test", ["invalid"], (), "source_unavailable"),
        ("https://charts.example.test:bad", ["8.8.8.8"], (), "source_unavailable"),
    ],
)
def test_transport_rejects_unsafe_destinations(
    url: str, addresses: list[str], hosts: tuple[str, ...], code: str
) -> None:
    """Check every DNS result before transport, including mixed public/private answers."""

    async def resolver(host: str) -> list[str]:
        """Return the specific hostile DNS result under test."""
        return addresses

    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any forbidden attempt to reach the transport."""
        called.append(request)
        return httpx.Response(200)

    http = MetadataHTTP(
        Settings(repository_allowed_hosts=hosts),
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SourceError, match=code):
        asyncio.run(http.get(url))
    assert not called


def test_transport_redirects_credentials_bounds_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strip cross-origin credentials and bound redirects, bytes and transport failures."""

    async def scenario() -> None:
        """Exercise real request construction with deterministic byte streams."""
        seen = []

        def redirect(request: httpx.Request) -> httpx.Response:
            """Redirect through both same-origin and cross-origin endpoints."""
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(302, headers={"location": "/again"})
            if len(seen) == 2:
                assert request.headers["authorization"] == "Bearer protected"
                return httpx.Response(307, headers={"location": "https://other.example.test/end"})
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"ok")

        http = MetadataHTTP(
            Settings(), resolver=public_dns, transport=httpx.MockTransport(redirect)
        )
        assert (
            await http.get(
                "https://charts.example.test", headers={"authorization": "Bearer protected"}
            )
        ).body == b"ok"
        for response, code in (
            (httpx.Response(302), "invalid_source_redirect"),
            (httpx.Response(302, headers={"location": "/loop"}), "source_redirect_limit"),
            (httpx.Response(200, headers={"content-length": "2000"}), "source_metadata_too_large"),
            (httpx.Response(200, headers={"content-length": "invalid"}), "source_unavailable"),
            (httpx.Response(200, content=b"a" * 2000), "source_metadata_too_large"),
        ):
            test = MetadataHTTP(
                Settings(repository_max_bytes=1024),
                resolver=public_dns,
                transport=httpx.MockTransport(lambda request, response=response: response),
            )
            with pytest.raises(SourceError, match=code):
                await test.get("https://charts.example.test")

        class Stream(httpx.AsyncByteStream):
            """Hide content length so the streaming byte bound must protect memory."""

            async def __aiter__(self) -> AsyncIterator[bytes]:
                """Yield individually small chunks whose aggregate exceeds the bound."""
                yield b"a" * 800
                yield b"b" * 800

        test = MetadataHTTP(
            Settings(repository_max_bytes=1024),
            resolver=public_dns,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
        )
        with pytest.raises(SourceError, match="source_metadata_too_large"):
            await test.get("https://charts.example.test")

        async def failed(host: str) -> list[str]:
            """Model a real resolver outage without waiting on the network."""
            raise OSError("never expose source details")

        with pytest.raises(SourceError, match="source_unavailable"):
            await MetadataHTTP(Settings(), resolver=failed).get("https://charts.example.test")

        async def timeout(host: str) -> list[str]:
            """Allow operation-wide timeout cancellation to interrupt resolution."""
            await asyncio.sleep(1)
            return ["8.8.8.8"]

        with pytest.raises(SourceError, match="source_timeout"):
            await RepositoryClient(
                MetadataHTTP(Settings(repository_timeout_seconds=0.001), resolver=timeout)
            ).fetch("helm_repository", "https://charts.example.test")

        def info(*args: object, **kwargs: object) -> list[tuple]:
            """Exercise the stdlib async resolver boundary with fixed socket answers."""
            return [
                (10, 1, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ]

        monkeypatch.setattr("socket.getaddrinfo", info)
        assert await resolve_public_host("charts.example.test") == [
            "8.8.8.8",
            "2606:4700:4700::1111",
        ]

    asyncio.run(scenario())


def oci_handler(request: httpx.Request) -> httpx.Response:
    """Serve a public OCI catalog, tags, manifest and config; never serve an archive."""
    config = json.dumps({"name": "example", "version": "1.0.0"}).encode()
    if request.url.path == "/v2/_catalog":
        return httpx.Response(200, json={"repositories": ["team/example", "other/ignored"]})
    if request.url.path.endswith("/tags/list"):
        return httpx.Response(200, json={"tags": ["1.0.0"]})
    if "/manifests/" in request.url.path:
        return httpx.Response(
            200,
            json={
                "schemaVersion": 2,
                "config": {
                    "mediaType": CHART_CONFIG,
                    "digest": "sha256:" + hashlib.sha256(config).hexdigest(),
                },
                "layers": [{"mediaType": CHART_LAYER, "digest": DIGEST}],
            },
        )
    assert "/blobs/" in request.url.path
    return httpx.Response(200, content=config)


def test_oci_discovery_known_paths_and_anonymous_tokens() -> None:
    """Handle ordinary catalogs and anonymous-token registries that hide their catalog."""
    result = asyncio.run(
        client(oci_handler).fetch("oci_registry", "oci://registry.example.test/team")
    )
    assert result.discovery_supported and result.versions[0].chart == "example"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Challenge public reads while exposing no operator credential requirement."""
        seen.append(request)
        if request.url.path == "/v2/_catalog":
            return httpx.Response(404)
        if request.url.path == "/token":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"access_token": "anonymous-pull"})
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer realm="https://auth.example.test/token",service="registry.example.test"'
                },
            )
        assert request.headers["authorization"] == "Bearer anonymous-pull"
        return oci_handler(request)

    result = asyncio.run(
        client(handler).fetch("oci_registry", "oci://registry.example.test/team", ("example",))
    )
    assert not result.discovery_supported and len(result.versions) == 1
    assert all("chart.content" not in request.url.path for request in seen)
    with pytest.raises(SourceError, match="chart_discovery_unavailable"):
        asyncio.run(client(handler).fetch("oci_registry", "oci://registry.example.test/team"))


def test_optional_credentials_are_used_without_quota_switching() -> None:
    """Use supplied credentials immediately and trust cross-host token services explicitly."""
    credentials = RepositoryCredentials(
        username="account", password="never-print-this", token_host="auth.example.test"
    )
    expected = "Basic " + base64.b64encode(b"account:never-print-this").decode()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Assert account usage at initial registry and authorized token-service requests."""
        seen.append(request)
        if request.url.path == "/token":
            assert request.headers["authorization"] == expected
            return httpx.Response(200, json={"token": "account-pull"})
        if request.headers.get("authorization") != "Bearer account-pull":
            assert request.headers["authorization"] == expected
            return httpx.Response(
                401, headers={"www-authenticate": 'Bearer realm="https://auth.example.test/token"'}
            )
        return oci_handler(request)

    result = asyncio.run(
        client(handler, repository_credentials={"registry.example.test": credentials}).fetch(
            "oci_registry", "oci://registry.example.test/team"
        )
    )
    assert result.versions[0].digest == DIGEST and seen
    assert "never-print-this" not in repr(
        Settings(repository_credentials={"registry.example.test": credentials})
    )
    credentials.token_host = None
    with pytest.raises(SourceError, match="untrusted_authentication_host"):
        asyncio.run(
            client(handler, repository_credentials={"registry.example.test": credentials}).fetch(
                "oci_registry", "oci://registry.example.test/team"
            )
        )


@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(401, headers={"www-authenticate": 'Bearer service="x"'}),
            "invalid_registry_authentication",
        ),
        (
            httpx.Response(401, headers={"www-authenticate": 'Basic realm="x"'}),
            "source_unavailable",
        ),
    ],
)
def test_invalid_registry_challenges(response: httpx.Response, code: str) -> None:
    """Reject unsupported or incomplete authentication without leaking challenge content."""

    async def scenario() -> None:
        """Read a named chart so discovery fallback is not mistaken for authentication success."""
        c = client(lambda request, response=response: response)
        with pytest.raises(SourceError, match=code):
            await c.fetch("oci_registry", "oci://registry.example.test/team", ("example",))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "token_response",
    [
        httpx.Response(403),
        httpx.Response(200, content=b"invalid"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"token": ""}),
        httpx.Response(200, json={"token": "x" * 8193}),
    ],
)
def test_invalid_token_responses(token_response: httpx.Response) -> None:
    """Keep token failures fixed and safe, never copying upstream body values into errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the controlled token failure after a valid public challenge."""
        return (
            token_response
            if request.url.path == "/token"
            else httpx.Response(
                401, headers={"www-authenticate": 'Bearer realm="https://auth.example.test/token"'}
            )
        )

    with pytest.raises(SourceError, match="source_authentication_failed"):
        asyncio.run(client(handler).fetch("oci_registry", "oci://registry.example.test/team"))


def test_oci_pagination_limits_and_malformed_metadata() -> None:
    """Bound discovery pages and reject hostile manifests, config blobs and names."""

    async def scenario() -> None:
        """Exercise parser and pagination boundaries without external I/O."""
        for response, code in (
            (httpx.Response(200, content=b"invalid"), "invalid_source_metadata"),
            (httpx.Response(200, json={"repositories": ["a", "b"]}), "source_item_limit"),
            (
                httpx.Response(200, json={"repositories": []}, headers={"link": "bad"}),
                "invalid_source_metadata",
            ),
            (
                httpx.Response(
                    200,
                    json={"repositories": []},
                    headers={"link": '<https://foreign.example.test/next>; rel="next"'},
                ),
                "source_not_allowed",
            ),
            (
                httpx.Response(
                    200, json={"repositories": []}, headers={"link": '</next>; rel="next"'}
                ),
                "source_page_limit",
            ),
        ):
            reader = client(lambda request, response=response: response, repository_max_items=1).oci
            with pytest.raises(SourceError, match=code):
                await reader.names(
                    "https://registry.example.test/v2/_catalog",
                    "repositories",
                    "registry:catalog:*",
                )
        calls = []

        def pages(request: httpx.Request) -> httpx.Response:
            """Return a valid two-page discovery sequence."""
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"repositories": ["team/example"]},
                    headers={"link": '</next>; rel="next"'},
                )
            return httpx.Response(200, json={"repositories": []})

        assert await client(pages).oci.names(
            "https://registry.example.test/v2/_catalog", "repositories", "registry:catalog:*"
        ) == ["team/example"]
        assert (
            await client(lambda request: httpx.Response(404)).oci.names(
                "https://registry.example.test/v2/example/tags/list",
                "tags",
                "repository:example:pull",
            )
            == []
        )
        for path, response, code in (
            (
                "/v2/_catalog",
                httpx.Response(200, json={"repositories": ["team/../bad"]}),
                "invalid_source_metadata",
            ),
            (
                "/tags/list",
                httpx.Response(200, json={"tags": ["bad/tag"]}),
                "invalid_source_metadata",
            ),
            ("/tags/list", httpx.Response(500), "source_unavailable"),
            ("/manifests/", httpx.Response(500), "source_unavailable"),
            ("/manifests/", httpx.Response(200, json={}), "invalid_source_metadata"),
            (
                "/manifests/",
                httpx.Response(
                    200,
                    json={
                        "schemaVersion": 2,
                        "config": {"mediaType": CHART_CONFIG, "digest": DIGEST},
                        "layers": [],
                    },
                ),
                "invalid_source_metadata",
            ),
            ("/blobs/", httpx.Response(200, content=b"tampered"), "source_digest_mismatch"),
        ):

            def handler(
                request: httpx.Request, path: str = path, response: httpx.Response = response
            ) -> httpx.Response:
                """Alter one protocol stage while keeping earlier responses valid."""
                return response if path in request.url.path else oci_handler(request)

            with pytest.raises(SourceError, match=code):
                await client(handler).fetch("oci_registry", "oci://registry.example.test/team")

        def nonchart(request: httpx.Request) -> httpx.Response:
            """Non-Helm OCI artifacts must not become chart versions."""
            if "/manifests/" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "schemaVersion": 2,
                        "config": {"mediaType": "application/other", "digest": DIGEST},
                        "layers": [],
                    },
                )
            return oci_handler(request)

        assert not (
            await client(nonchart).fetch("oci_registry", "oci://registry.example.test/team")
        ).versions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "config,tag,valid",
    [
        ({}, "1.0.0", False),
        ({"name": "other", "version": "1.0.0"}, "1.0.0", False),
        ({"name": "example", "version": "2.0.0"}, "1.0.0", False),
        ({"name": "example", "version": "latest"}, "latest", False),
        (
            {
                "name": "example",
                "version": "1.0.0+build",
                "description": "OCI upstream description",
                "icon": "https://charts.example.test/oci.svg",
            },
            "1.0.0_build",
            True,
        ),
    ],
)
def test_oci_verified_config_identity(config: dict[str, str], tag: str, valid: bool) -> None:
    """A correct blob hash does not make a malformed or mismatched Helm identity valid."""
    body = json.dumps(config).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve a hash-correct config while varying its declared chart identity."""
        if request.url.path.endswith("/tags/list"):
            return httpx.Response(200, json={"tags": [tag]})
        if "/manifests/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 2,
                    "config": {"mediaType": CHART_CONFIG, "digest": digest},
                    "layers": [{"mediaType": CHART_LAYER, "digest": DIGEST}],
                },
            )
        if "/blobs/" in request.url.path:
            return httpx.Response(200, content=body)
        return oci_handler(request)

    if valid:
        metadata = asyncio.run(
            client(handler).fetch("oci_registry", "oci://registry.example.test/team")
        ).versions[0]
        assert metadata.description == "OCI upstream description"
        assert metadata.icon == "https://charts.example.test/oci.svg"
        assert (
            asyncio.run(client(handler).fetch("oci_registry", "oci://registry.example.test/team"))
            .versions[0]
            .version
            == "1.0.0+build"
        )
    else:
        with pytest.raises(SourceError, match="invalid_source_metadata"):
            asyncio.run(client(handler).fetch("oci_registry", "oci://registry.example.test/team"))


def test_oci_total_observation_bound_across_charts() -> None:
    """Individual pages can be small while their combined version count exceeds the bound."""
    configs = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve two charts with two valid versions each from a registry root."""
        if request.url.path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["example", "another"]})
        if request.url.path.endswith("/tags/list"):
            return httpx.Response(200, json={"tags": ["1.0.0", "2.0.0"]})
        parts = request.url.path.split("/")
        if "/manifests/" in request.url.path:
            body = json.dumps({"name": parts[2], "version": parts[-1]}).encode()
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            configs[digest] = body
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 2,
                    "config": {"mediaType": CHART_CONFIG, "digest": digest},
                    "layers": [{"mediaType": CHART_LAYER, "digest": DIGEST}],
                },
            )
        return httpx.Response(200, content=configs[parts[-1]])

    with pytest.raises(SourceError, match="source_item_limit"):
        asyncio.run(
            client(handler, repository_max_items=2).fetch(
                "oci_registry", "oci://registry.example.test"
            )
        )
    assert (
        len(
            asyncio.run(
                client(handler, repository_max_items=4).fetch(
                    "oci_registry", "oci://registry.example.test"
                )
            ).versions
        )
        == 4
    )


def test_upstream_rate_limit_is_reported_without_retry() -> None:
    """An upstream quota response remains distinct from platform endpoint throttling."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a quota response without triggering a credential switch."""
        attempts.append(request)
        return httpx.Response(429)

    with pytest.raises(SourceError) as limited:
        asyncio.run(client(handler).fetch("helm_repository", "https://charts.example.test"))
    assert limited.value.status == 429 and limited.value.code == "source_rate_limited"
    assert len(attempts) == 1
