"""Independent application/release visibility and immutable versioned definitions."""

import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from chart_fixtures import seed_source
from fastapi.testclient import TestClient
from publication_fixtures import chart_archive
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from tinypods_api.main import create_app
from tinypods_core.applications.definitions import ReleaseState
from tinypods_core.applications.models import Release
from tinypods_core.auth import SessionService
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.db.models import User
from tinypods_core.repositories.verification import ChartPin, ChartVerification
from tinypods_core.settings import Settings

BASE = "/api/v1"


@dataclass
class CatalogAPI:
    """Real authenticated owner API and its disposable application/release identities."""

    client: TestClient
    headers: dict[str, str] = field(repr=False)
    application: str
    release: str

    @property
    def path(self) -> str:
        """Return the current release resource path."""
        return BASE + "/releases/" + self.release

    @property
    def app_path(self) -> str:
        """Return the parent application resource path."""
        return BASE + "/applications/" + self.application


@pytest.fixture
def catalog_api(auth_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[CatalogAPI]:
    """Compose real storage, sessions, validators and controlled chart transport."""
    body = chart_archive()

    async def download(self: ChartVerification, pin: ChartPin) -> bytes:
        assert pin.digest == "sha256:" + hashlib.sha256(body).hexdigest()
        return body

    monkeypatch.setattr(ChartVerification, "download_archive", download)

    async def initialize() -> None:
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    version = seed_source(
        auth_settings, version="1.0.0", digest="sha256:" + hashlib.sha256(body).hexdigest()
    )
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        assert (
            client.post(
                BASE + "/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": client.cookies["__Host-tinypods-csrf"]}
        app = client.post(
            BASE + "/applications",
            json={"name": "Example", "slug": "example", "status": "stable"},
            headers=headers,
        )
        assert app.status_code == 201, app.text
        release = client.post(
            BASE + "/releases",
            json={
                "application_id": app.json()["id"],
                "chart_version_id": version,
                "architectures": ["arm64"],
                "state": "in_review",
            },
            headers=headers,
        )
        assert release.status_code == 201, release.text
        yield CatalogAPI(client, headers, app.json()["id"], release.json()["id"])


@contextmanager
def ordinary_client(settings: Settings) -> Iterator[TestClient]:
    """Issue an ordinary real session in an independent application lifecycle."""

    async def initialize() -> str:
        database = Database(settings)
        try:
            async with database.transaction() as session:
                user = User(username="reader-" + uuid4().hex)
                session.add(user)
            return (await SessionService(database).issue(user.id)).token
        finally:
            await database.close()

    token = asyncio.run(initialize())
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        client.cookies.set("__Host-tinypods-session", token)
        yield client


def test_independent_visibility_states(catalog_api: CatalogAPI, auth_settings: Settings) -> None:
    """Expose a release only when both its own state and its application's state allow it."""
    api = catalog_api
    with ordinary_client(auth_settings) as reader:
        for path in (api.app_path, api.path, api.app_path + "/categories"):
            assert reader.get(path).status_code == 404
        assert reader.get(BASE + "/applications").json()["total"] == 0
        assert reader.get(BASE + "/releases").json()["total"] == 0
        assert api.client.get(api.app_path).json()["state"] == "draft"
        assert api.client.get(api.path).json()["state"] == "in_review"
        assert api.client.post(api.path + "/publication").status_code == 403
        preview = api.client.post(api.path + "/validation", headers=api.headers)
        assert preview.json() == {"valid": True, "issues": []}
        published = api.client.post(api.path + "/publication", headers=api.headers)
        assert published.status_code == 200, published.text
        data = published.json()
        assert data["state"] == "published" and data["published_at"]
        assert "snapshot" not in data and "publication" not in data
        assert reader.get(api.path).status_code == 404
        assert (
            api.client.patch(
                api.app_path, json={"state": "in_review"}, headers=api.headers
            ).status_code
            == 200
        )
        assert reader.get(api.app_path).status_code == 404
        app = api.client.patch(api.app_path, json={"state": "published"}, headers=api.headers)
        assert app.json()["state"] == "published" and app.json()["status"] == "stable"
        assert reader.get(api.app_path).status_code == 200
        assert reader.get(api.path).status_code == 200
        assert reader.get(BASE + "/applications?state=published").json()["total"] == 1
        assert reader.get(BASE + "/releases").json()["total"] == 1
        assert (
            api.client.patch(api.path, json={"state": "draft"}, headers=api.headers).status_code
            == 200
        )
        assert reader.get(api.path).status_code == 404
        assert reader.get(api.app_path).status_code == 200
        assert (
            api.client.patch(
                api.path, json={"architectures": ["amd64"]}, headers=api.headers
            ).status_code
            == 409
        )
        assert (
            api.client.patch(api.path, json={"state": "in_review"}, headers=api.headers).status_code
            == 200
        )
        assert (
            api.client.post(api.path + "/publication", headers=api.headers).json()["published_at"]
            == data["published_at"]
        )
        assert (
            api.client.patch(api.app_path, json={"state": "draft"}, headers=api.headers).status_code
            == 200
        )
        assert reader.get(api.path).status_code == 404
        assert reader.get(BASE + "/releases").json()["total"] == 0
        assert (
            api.client.patch(
                api.app_path, json={"state": "published"}, headers=api.headers
            ).status_code
            == 200
        )
        for path in (api.path, api.app_path):
            assert (
                api.client.patch(path, json={"state": "invalid"}, headers=api.headers).status_code
                == 422
            )
        assert (
            api.client.patch(api.path, json={"state": "published"}, headers=api.headers).status_code
            == 422
        )
        assert api.client.get(api.path + "/publication").status_code == 405


def test_clone_lifecycle_and_direct_database_protection(
    catalog_api: CatalogAPI, auth_settings: Settings
) -> None:
    """Keep published deployment fields immutable even after unpublishing; clone to edit."""
    api = catalog_api
    original = api.client.post(api.path + "/publication", headers=api.headers)
    assert original.status_code == 200, original.text
    clone = api.client.post(api.path + "/clone", headers=api.headers)
    assert clone.status_code == 200, clone.text
    cloned = clone.json()
    assert cloned["revision"] == 2 and cloned["state"] == "draft" and cloned["published_at"] is None
    path = BASE + "/releases/" + cloned["id"]
    assert (
        api.client.patch(path, json={"architectures": []}, headers=api.headers).status_code == 200
    )
    invalid = api.client.post(path + "/publication", headers=api.headers)
    assert (
        invalid.status_code == 422
        and invalid.json()["issues"][0]["code"] == "invalid_architectures"
    )
    assert (
        api.client.patch(
            path,
            json={
                "architectures": ["arm64"],
                "configuration_fields": [
                    {
                        "label": "Replicas",
                        "path": "replicas",
                        "type": "number",
                        "set_by": "administrator",
                        "value": 2,
                    }
                ],
            },
            headers=api.headers,
        ).status_code
        == 200
    )
    assert api.client.post(path + "/publication", headers=api.headers).status_code == 200
    assert api.client.get(api.path).json() == original.json()
    assert (
        api.client.patch(
            api.path, json={"notes": "Updated documentation"}, headers=api.headers
        ).status_code
        == 200
    )
    assert api.client.delete(api.path, headers=api.headers).status_code == 409
    assert api.client.post(
        api.path + "/deprecation", json={"reason": "Superseded"}, headers=api.headers
    ).json()["deprecated"]
    revoked = api.client.post(
        api.path + "/revocation", json={"reason": "Withdrawn"}, headers=api.headers
    )
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert api.client.post(api.path + "/publication", headers=api.headers).status_code == 409

    async def protect() -> None:
        database = Database(auth_settings)
        try:
            for statement in (
                "UPDATE releases SET architectures='[\"amd64\"]'::jsonb WHERE id=:id",
                "UPDATE releases SET published_at=NULL WHERE id=:id",
                "UPDATE releases SET revoked_at=NULL WHERE id=:id",
            ):
                with pytest.raises(IntegrityError):
                    async with database.transaction() as session:
                        await session.exec(text(statement), params={"id": UUID(api.release)})
            async with database.session() as session:
                row = await session.get(Release, UUID(api.release))
                assert row is not None and row.configuration_fields == []
                assert isinstance(row.state, ReleaseState)
                assert (
                    await session.exec(text("SELECT to_regclass('release_publications')"))
                ).scalar_one() is None
            for table in ("applications", "releases"):
                with pytest.raises(IntegrityError):
                    async with database.transaction() as session:
                        await session.exec(text("UPDATE " + table + " SET state='invalid'"))
        finally:
            await database.close()

    asyncio.run(protect())


def test_atomic_publication_races_and_rollback(auth_settings: Settings) -> None:
    """Serialize simultaneous publishers/clones and roll back failed audited transitions."""
    from collections.abc import Awaitable
    from dataclasses import replace
    from datetime import UTC, datetime

    from sqlmodel import select
    from tinypods_core.applications.models import Application
    from tinypods_core.applications.publication import DraftState, ReleasePublicationService
    from tinypods_core.applications.service import DefinitionError, DefinitionService
    from tinypods_core.applications.validation import (
        PublicationValidator,
        ReleaseDraft,
        ValidatedDefinition,
        validate_definition,
    )
    from tinypods_core.audit import AuditRecord
    from tinypods_core.auth import AuthError, Principal
    from tinypods_core.auth.authorization import AuthorizationService
    from tinypods_core.repositories.client import RepositoryClient
    from tinypods_core.repositories.sources import SourceError
    from tinypods_core.repositories.transport import MetadataHTTP

    body = chart_archive()
    version = UUID(
        seed_source(
            auth_settings, version="1.0.0", digest="sha256:" + hashlib.sha256(body).hexdigest()
        )
    )

    class Validator:
        async def validate_principal(self, principal: Principal) -> Principal:
            return principal

    class CPUValidator(PublicationValidator):
        async def validate_admitted(
            self, payload: bytes, draft: ReleaseDraft
        ) -> ValidatedDefinition:
            return validate_definition(payload, draft)

    class Charts(ChartVerification):
        async def download_archive(self, pin: ChartPin) -> bytes:
            return body

    async def scenario() -> None:
        database = Database(auth_settings)
        owner = Principal(uuid4(), "owner", True, "test", credential_kind="test")
        reader = replace(owner, is_owner=False)
        authorization = AuthorizationService(Validator())
        charts = Charts(database, authorization, RepositoryClient(MetadataHTTP(auth_settings)))
        service = ReleasePublicationService(database, authorization, charts, CPUValidator())

        async def draft() -> UUID:
            async with database.transaction() as session:
                application = Application(name="Example", slug="app-" + uuid4().hex)
                session.add(application)
                await session.flush()
                release = Release(
                    application_id=application.id, chart_version_id=version, architectures=["arm64"]
                )
                session.add(release)
                await session.flush()
                return release.id

        async def error(status: int, action: Awaitable[object]) -> None:
            with pytest.raises(DefinitionError) as failure:
                await action
            assert failure.value.status == status

        try:
            missing = uuid4()
            await error(404, service.draft(missing))
            await error(404, service.publish(owner, missing))
            await error(404, service.clone(owner, missing))
            await error(404, service.lifecycle(owner, missing, "reason", revoke=True))
            for reason in (" ", "x" * 1001):
                await error(422, service.lifecycle(owner, missing, reason, revoke=True))
            await error(422, service.lifecycle(owner, missing, "reason"))
            identity = await draft()
            await error(409, service.lifecycle(owner, identity, "reason", revoke=True))
            with pytest.raises(AuthError):
                await service.publish(reader, identity)
            async with database.session() as session:
                await error(
                    422,
                    DefinitionService(Release, session, authorization).save(
                        owner, {"state": "published"}, identity=identity
                    ),
                )
            # Both candidates finish verification before either acquires the write lock.
            reached = 0
            ready = asyncio.Event()
            original = service.checked

            async def checked(principal: Principal, state: DraftState) -> ValidatedDefinition:
                nonlocal reached
                result = await original(principal, state)
                reached += 1
                if reached == 2:
                    ready.set()
                await ready.wait()
                return result

            service.checked = checked
            first, second = await asyncio.wait_for(
                asyncio.gather(service.publish(owner, identity), service.publish(owner, identity)),
                10,
            )
            assert first == second == identity
            service.checked = original
            assert await service.publish(owner, identity) == identity
            await error(409, service.draft(identity))
            clones = await asyncio.gather(
                service.clone(owner, identity), service.clone(owner, identity)
            )
            async with database.session() as session:
                records = [await session.get(Release, item) for item in clones]
                assert sorted(item.revision for item in records) == [2, 3]
                events = (
                    await session.exec(
                        select(AuditRecord).where(AuditRecord.changed_object_id == str(identity))
                    )
                ).all()
                assert sum(row.postchange_data.get("state") == "published" for row in events) == 1
            await service.lifecycle(owner, identity, "deprecated", deprecated=True)
            await service.lifecycle(owner, identity, "restored", deprecated=False)
            await service.lifecycle(owner, identity, "revoked", revoke=True)
            await service.lifecycle(owner, identity, "still revoked", revoke=True)
            await error(409, service.publish(owner, identity))

            changed = await draft()

            async def edit_after_check(
                principal: Principal, state: DraftState
            ) -> ValidatedDefinition:
                result = await original(principal, state)
                async with database.transaction() as session:
                    record = await session.get(Release, state.definition.release_id)
                    record.architectures = (
                        ["amd64"] if record.architectures == ["arm64"] else ["arm64"]
                    )
                    session.add(record)
                return result

            service.checked = edit_after_check
            await error(409, service.publish(owner, changed))
            await error(409, service.validate(owner, changed))
            service.checked = original

            deleted = await draft()

            async def delete_after_check(
                principal: Principal, state: DraftState
            ) -> ValidatedDefinition:
                result = await original(principal, state)
                async with database.transaction() as session:
                    await session.delete(await session.get(Release, state.definition.release_id))
                return result

            service.checked = delete_after_check
            await error(404, service.publish(owner, deleted))
            service.checked = original

            revoked = await draft()

            async def revoke_after_check(
                principal: Principal, state: DraftState
            ) -> ValidatedDefinition:
                result = await original(principal, state)
                async with database.transaction() as session:
                    record = await session.get(Release, state.definition.release_id)
                    record.revoked_at = datetime.now(UTC)
                    session.add(record)
                return result

            service.checked = revoke_after_check
            await error(409, service.publish(owner, revoked))
            service.checked = original

            # A source change after the old check must still prevent publication.
            for statement, restore in (
                (
                    "UPDATE chart_repositories SET enabled=false",
                    "UPDATE chart_repositories SET enabled=true",
                ),
                (
                    "UPDATE chart_repositories SET url='https://changed.example.test'",
                    "UPDATE chart_repositories SET url='https://charts.example.test'",
                ),
            ):
                candidate = await draft()

                async def source_after_check(
                    principal: Principal, state: DraftState, statement: str = statement
                ) -> ValidatedDefinition:
                    result = await original(principal, state)
                    async with database.transaction() as session:
                        await session.exec(text(statement))
                    return result

                service.checked = source_after_check
                await error(409, service.publish(owner, candidate))
                service.checked = original
                async with database.transaction() as session:
                    await session.exec(text(restore))
                    row = await session.get(Release, candidate)
                    assert row.state == "draft" and row.published_at is None

            # Shared source locks survive until the final transaction ends.
            from sqlalchemy.exc import DBAPIError

            async with database.transaction() as session:
                assert await charts.lock_pin(session, version) == await charts.pin(version)
                with pytest.raises(DBAPIError) as blocked:
                    async with database.transaction() as writer:
                        await writer.exec(text("SET LOCAL lock_timeout='50ms'"))
                        await writer.exec(text("UPDATE chart_repositories SET enabled=false"))
                assert blocked.value.orig.sqlstate == "55P03"
            async with database.transaction() as session:
                await error(404, charts.lock_pin(session, uuid4()))

            # A real database failure leaves no published state or publication audit.
            rollback = await draft()
            async with database.transaction() as session:
                await session.exec(
                    text(
                        "CREATE FUNCTION reject_test_publish() RETURNS trigger "
                        "LANGUAGE plpgsql AS $$ "
                        "BEGIN IF NEW.state='published' THEN RAISE EXCEPTION 'test rejection' "
                        "USING ERRCODE='23514'; END IF; RETURN NEW; END $$"
                    )
                )
                await session.exec(
                    text(
                        "CREATE TRIGGER reject_test_publish BEFORE UPDATE ON releases "
                        "FOR EACH ROW EXECUTE FUNCTION reject_test_publish()"
                    )
                )
            await error(409, service.publish(owner, rollback))
            async with database.transaction() as session:
                await session.exec(text("DROP TRIGGER reject_test_publish ON releases"))
                await session.exec(text("DROP FUNCTION reject_test_publish()"))
                record = await session.get(Release, rollback)
                assert record.state == "draft" and record.published_at is None
            state = await service.draft(rollback)
            changed_pin = replace(state.definition.chart, digest="sha256:" + "f" * 64)
            await error(
                409,
                service.checked(
                    owner, replace(state, definition=replace(state.definition, chart=changed_pin))
                ),
            )

            async def timeout(pin: ChartPin) -> bytes:
                raise TimeoutError

            charts.download_archive = timeout
            with pytest.raises(SourceError, match="source_timeout"):
                await service.checked(owner, state)
        finally:
            await database.close()

    asyncio.run(scenario())


def test_publication_dependency_requires_database() -> None:
    """Reject publication cleanly when runtime database services are disabled."""
    from types import SimpleNamespace

    from fastapi import HTTPException
    from tinypods_api.applications.dependencies import publication_service

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=None)))
    with pytest.raises(HTTPException) as failure:
        publication_service(request, None)
    assert failure.value.status_code == 503
