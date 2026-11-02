"""Verified archive verification regressions using controlled external HTTP fixtures."""

import asyncio
import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import replace
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from chart_fixtures import seed_source
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from test_repository_sources import client
from tinypods_api.main import create_app
from tinypods_core.applications.service import DefinitionError
from tinypods_core.auth import AuthError, SessionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.repositories import archive as module
from tinypods_core.repositories.archive import document, inspect_chart
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.verification import ChartPin, ChartVerification, archive_url
from tinypods_core.settings import Settings


def package(files: dict[str, bytes], kind: bytes = tarfile.REGTYPE) -> bytes:
    """Create controlled tar members without touching the filesystem."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.type = kind
            member.linkname = "outside"
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def pin(body: bytes, source: str = "helm_repository") -> ChartPin:
    """Pin exact fixture bytes and source identity."""
    return ChartPin(
        source,
        "https://charts.example.test/team",
        "example",
        "1.0.0",
        "sha256:" + hashlib.sha256(body).hexdigest(),
    )


def inspect(body: bytes) -> module.ChartFacts:
    """Inspect fixture bytes using their exact trusted digest."""
    identity = pin(body)
    return inspect_chart(body, identity.name, identity.version, identity.digest)


def valid() -> dict[str, bytes]:
    """Include ordinary nested defaults and an optional JSON values schema."""
    return {
        "example/Chart.yaml": b"apiVersion: v2\nname: example\nversion: 1.0.0\n",
        "example/values.yaml": b"replicas: 1\nservice: {enabled: true, ports: [80, 443]}\n",
        "example/values.schema.json": b'{"type":"object"}',
    }


def test_verified_archive_facts_and_optional_documents() -> None:
    """Return exact configuration without evaluating templates or nested packages."""
    files = valid()
    files["example/templates/pod.yaml"] = b"{{ malicious template is never executed }}"
    files["example/charts/dependency.tgz"] = b"opaque nested bytes"
    facts = inspect(package(files))
    assert facts.name == "example" and facts.defaults["replicas"] == 1
    assert facts.values_schema == {"type": "object"} and facts.files == 5
    assert inspect(package({"example/Chart.yaml": files["example/Chart.yaml"]})).defaults == {}


@pytest.mark.parametrize(
    "name,kind,code",
    [
        ("../outside", tarfile.REGTYPE, "unsafe_chart_path"),
        ("/example/file", tarfile.REGTYPE, "unsafe_chart_path"),
        ("example/a/../file", tarfile.REGTYPE, "unsafe_chart_path"),
        ("other/Chart.yaml", tarfile.REGTYPE, "unexpected_chart_root"),
        ("example/link", tarfile.SYMTYPE, "unsupported_chart_member"),
        ("example/link", tarfile.LNKTYPE, "unsupported_chart_member"),
        ("example/device", tarfile.CHRTYPE, "unsupported_chart_member"),
        ("example/a:b", tarfile.REGTYPE, "unsafe_chart_path"),
        ("example/a\\b", tarfile.REGTYPE, "unsafe_chart_path"),
    ],
)
def test_unsafe_members(name: str, kind: bytes, code: str) -> None:
    """Reject paths, links and devices even though inspection never extracts files."""
    with pytest.raises(SourceError, match=code):
        inspect(package({name: b"bad"}, kind))


@pytest.mark.parametrize("body", [b"not gzip", gzip.compress(b"not tar"), b"\x1f\x8b"])
def test_corrupt_archives(body: bytes) -> None:
    """Fail safely on gzip or tar corruption after digest verification."""
    with pytest.raises(SourceError, match="invalid_chart_archive"):
        inspect(body)


def test_identity_digest_and_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject changed bytes and enforce actual compressed/expanded/document/file bounds."""
    body = package(valid())
    with pytest.raises(SourceError, match="source_digest_mismatch"):
        inspect_chart(body, "example", "1.0.0", "sha256:" + "0" * 64)
    with pytest.raises(SourceError, match="chart_identity_mismatch"):
        inspect_chart(body, "example", "2.0.0", pin(body).digest)
    with pytest.raises(SourceError, match="invalid_chart_archive"):
        inspect(package({"example/values.yaml": b"{}"}))
    for constant, maximum, code in [
        ("MAX_ARCHIVE_BYTES", 10, "chart_archive_too_large"),
        ("MAX_EXPANDED_BYTES", 100, "chart_expansion_limit"),
        ("MAX_DOCUMENT_BYTES", 10, "chart_document_too_large"),
        ("MAX_FILES", 1, "chart_file_limit"),
        ("MAX_DEPTH", 1, "chart_path_depth_limit"),
    ]:
        with monkeypatch.context() as patch:
            patch.setattr(module, constant, maximum)
            with pytest.raises(SourceError, match=code):
                inspect(body)


@pytest.mark.parametrize(
    "body",
    [
        b"a: &a {b: *a}",
        b"a: 1\na: 2",
        b"!!python/object/apply:os.system [bad]",
        b"a: .nan",
        b"[1,2]",
        b"a: 2026-01-01",
        b"{1: value}",
        b"a: [",
        b"a: " + b"[" * 34 + b"]" * 34,
        b"\xff",
    ],
)
def test_invalid_documents(body: bytes) -> None:
    """Never execute unsafe YAML or expose values in parse failures."""
    with pytest.raises(SourceError, match="invalid_chart_document"):
        document(body)


def test_document_json_types_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep finite JSON scalars and reject non-JSON/deep mappings with fixed errors."""
    assert document(b"null_value: null\nfloat: 1.5\nstring: text") == {
        "null_value": None,
        "float": 1.5,
        "string": "text",
    }
    with pytest.raises(SourceError, match="invalid_chart_document"):
        module.json_value({}, 33)
    monkeypatch.setattr(module, "MAX_DOCUMENT_BYTES", 2)
    with pytest.raises(SourceError, match="chart_document_too_large"):
        document(b"key: value")


def index(identity: ChartPin, urls: object = None) -> bytes:
    """Build upstream metadata for the exact fixture archive."""
    return json.dumps(
        {
            "entries": {
                "example": [
                    {
                        "name": "example",
                        "version": identity.version,
                        "digest": identity.digest.removeprefix("sha256:"),
                        "urls": ["example.tgz"] if urls is None else urls,
                    }
                ]
            }
        }
    ).encode()


def test_exact_helm_url_selection() -> None:
    """A changed pin cannot silently select latest or another digest."""
    identity = pin(package(valid()))
    assert (
        archive_url(
            index(identity), identity.name, identity.version, identity.digest, identity.url, 10
        )
        == identity.url + "/example.tgz"
    )
    with pytest.raises(SourceError, match="pinned_chart_unavailable"):
        archive_url(index(identity), "example", "2.0.0", identity.digest, identity.url, 10)
    for urls in ([], "string", [123]):
        with pytest.raises(SourceError, match="invalid_source_metadata"):
            archive_url(
                index(identity, urls),
                "example",
                identity.version,
                identity.digest,
                identity.url,
                10,
            )
    data = json.loads(index(identity))
    data["entries"]["example"].append({**data["entries"]["example"][0], "urls": ["other.tgz"]})
    with pytest.raises(SourceError, match="ambiguous_chart_archive"):
        archive_url(
            json.dumps(data).encode(),
            "example",
            identity.version,
            identity.digest,
            identity.url,
            10,
        )


@pytest.mark.parametrize("source", ["helm_repository", "oci_registry"])
def test_helm_and_oci_exact_bytes(source: str) -> None:
    """Use real async transport, preserving DNS pinning and exact OCI layer addresses."""
    body = package(valid())
    identity = replace(
        pin(body),
        source_type=source,
        url=(
            "oci://registry.example.test/team"
            if source == "oci_registry"
            else "https://charts.example.test/team"
        ),
    )
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Serve controlled public metadata and immutable archive bytes."""
        seen.append(request.url.path)
        if request.url.path.endswith("index.yaml"):
            return httpx.Response(200, content=index(identity))
        return httpx.Response(200, content=body)

    service = ChartVerification(None, None, client(handler))
    facts = asyncio.run(service.fetch(identity))
    assert facts.digest == identity.digest
    assert seen[-1].endswith(identity.digest if source == "oci_registry" else "example.tgz")


@pytest.mark.parametrize("status,index_failure", [(404, False), (403, False), (404, True)])
def test_external_failures(status: int, index_failure: bool) -> None:
    """Unavailable pinned content never returns substitute chart facts."""
    identity = pin(package(valid()))

    async def handler(request: httpx.Request) -> httpx.Response:
        """Fail either the index or archive boundary."""
        if request.url.path.endswith("index.yaml") and not index_failure:
            return httpx.Response(200, content=index(identity))
        return httpx.Response(status)

    with pytest.raises(
        SourceError, match="source_unavailable" if index_failure else "pinned_chart_unavailable"
    ):
        asyncio.run(ChartVerification(None, None, client(handler)).fetch(identity))


def test_verification_api_and_source_changes(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforce owner/CSRF policies, detached pins and changes during external I/O."""
    body = package(valid())
    identity = pin(body)
    version_id = UUID(seed_source(auth_settings, version="1.0.0", digest=identity.digest))

    async def bootstrap() -> None:
        """Create the disposable test owner using the real bootstrap path."""
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(bootstrap())
    original_fetch = ChartVerification.fetch
    mode = "success"

    async def fetch(self: ChartVerification, current: ChartPin) -> module.ChartFacts:
        """Inject only external waits while exercising real DB/auth/API behavior."""
        if mode == "timeout":
            raise TimeoutError
        if mode == "cancel":
            raise asyncio.CancelledError
        if mode == "revoke":
            async with self.database.transaction() as session:
                await session.exec(text("UPDATE auth_session SET revoked_at=now()"))
        if mode == "change":
            async with self.database.transaction() as session:
                await session.exec(
                    text("UPDATE chart_repositories SET url='https://changed.example.test'")
                )
        return inspect_chart(body, current.name, current.version, current.digest)

    monkeypatch.setattr(ChartVerification, "fetch", fetch)
    path = "/api/v1/chart-versions/" + str(version_id) + "/verify"
    with TestClient(create_app(auth_settings), base_url="https://testserver") as browser:
        assert browser.post(path).status_code == 401
        assert (
            browser.post(
                "/api/v1/auth/login",
                json={"username": "Owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": browser.cookies["__Host-tinypods-csrf"]}
        assert browser.post(path).status_code == 403
        response = browser.post(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["digest"] == identity.digest
        assert response.json()["defaults"]["replicas"] == 1
        assert (
            browser.post(
                "/api/v1/chart-versions/" + str(uuid4()) + "/verify", headers=headers
            ).status_code
            == 404
        )
        mode = "timeout"
        assert browser.post(path, headers=headers).json()["code"] == "source_timeout"
        mode = "success"

    async def scenario() -> None:
        """Exercise cancellation, revalidation and disabled sources outside HTTP budgets."""
        nonlocal mode
        database = Database(auth_settings)
        sessions = SessionService(database)
        try:
            async with database.session() as session:
                owner = (await session.exec(text("SELECT id FROM auth_user"))).one()[0]
            issued = await sessions.issue(owner)
            principal = await sessions.authenticate(issued.token)
            authorization = AuthorizationService(sessions)
            service = ChartVerification(
                database, authorization, client(lambda request: httpx.Response(200))
            )
            with pytest.raises(DefinitionError) as absent:
                await service.pin(uuid4())
            assert absent.value.status == 404
            mode = "cancel"
            with pytest.raises(asyncio.CancelledError):
                await service.run(principal, version_id)
            mode = "change"
            with pytest.raises(DefinitionError) as changed:
                await service.run(principal, version_id)
            assert changed.value.status == 409
            async with database.transaction() as session:
                await session.exec(text("UPDATE chart_repositories SET enabled=false"))
            with pytest.raises(DefinitionError) as disabled:
                await service.pin(version_id)
            assert disabled.value.status == 409
            async with database.transaction() as session:
                await session.exec(text("UPDATE chart_repositories SET enabled=true"))
            mode = "success"
            from tinypods_api.repositories.routes import verify_version

            assert (await verify_version(version_id, service, principal)).digest == identity.digest
            mode = "revoke"
            with pytest.raises(AuthError):
                await service.run(principal, version_id)
            with pytest.raises(AuthError):
                await service.run(principal, version_id)
        finally:
            await database.close()

    asyncio.run(scenario())
    monkeypatch.setattr(ChartVerification, "fetch", original_fetch)


def test_complex_mapping_keys_and_directory_document() -> None:
    """Reject complex YAML keys and a directory masquerading as Chart.yaml."""
    with pytest.raises(SourceError, match="invalid_chart_document"):
        document(b"? [a, b]\n: value")
    with pytest.raises(SourceError, match="invalid_chart_archive"):
        inspect(package({"example/Chart.yaml": b""}, tarfile.DIRTYPE))


def test_index_with_prefixed_digest_and_other_versions() -> None:
    """Ignore nonmatching entries while requiring the exact prefixed digest."""
    identity = pin(package(valid()))
    data = json.loads(index(identity))
    current = data["entries"]["example"][0]
    current["digest"] = identity.digest
    data["entries"]["example"].insert(0, {**current, "version": "2.0.0"})
    assert archive_url(
        json.dumps(data).encode(), "example", "1.0.0", identity.digest, identity.url, 10
    ).endswith("example.tgz")


def test_empty_values_are_accepted() -> None:
    """Helm permits an empty values document."""
    files = valid()
    files["example/values.yaml"] = b"# no defaults\n"
    assert inspect(package(files)).defaults == {}


def test_redirected_index_relative_archive() -> None:
    """Resolve relative archives at the final index location, not the redirect origin."""
    body = package(valid())
    identity = pin(body)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Redirect the index while serving archive bytes only at its final base."""
        seen.append(request.url.path)
        if request.url.path == "/team/index.yaml":
            return httpx.Response(
                302, headers={"location": "https://cdn.example.test/final/index.yaml"}
            )
        if request.url.path == "/final/index.yaml":
            return httpx.Response(200, content=index(identity))
        assert request.url.path == "/final/example.tgz"
        return httpx.Response(200, content=body)

    assert (
        asyncio.run(ChartVerification(None, None, client(handler)).fetch(identity)).digest
        == identity.digest
    )
    assert seen == ["/team/index.yaml", "/final/index.yaml", "/final/example.tgz"]


def test_canonical_paths_directories_duplicates_and_declared_size() -> None:
    """Accept normal directories/relative spelling, but reject ambiguous names and sizes."""
    files = valid()
    files["./example/Chart.yaml"] = files.pop("example/Chart.yaml")
    assert inspect(package(files)).name == "example"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo("example/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        body = valid()["example/Chart.yaml"]
        for name in ("example/Chart.yaml", "./example/Chart.yaml"):
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    with pytest.raises(SourceError, match="duplicate_chart_path"):
        inspect(output.getvalue())
    for size in (-1, module.MAX_EXPANDED_BYTES + 1):
        member = tarfile.TarInfo("example/file")
        member.size = size
        body = gzip.compress(member.tobuf(format=tarfile.GNU_FORMAT) + bytes(1024))
        with pytest.raises(
            SourceError, match=("invalid_chart_archive" if size < 0 else "chart_member_size_limit")
        ):
            inspect(body)


@pytest.mark.parametrize("name", [".", "./", ""])
def test_empty_archive_paths_are_rejected_without_indexing(name: str) -> None:
    """Empty normalized paths must produce safe domain errors, never IndexError."""
    with pytest.raises(SourceError, match="unsafe_chart_path|invalid_chart_archive"):
        inspect(package({name: b""}, tarfile.DIRTYPE))


def test_valid_chart_root_directory_is_accepted() -> None:
    """An ordinary root directory followed by root metadata is a valid package."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo("example/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        body = valid()["example/Chart.yaml"]
        member = tarfile.TarInfo("example/Chart.yaml")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    assert inspect(output.getvalue()).name == "example"
