"""Application API behavior through real PostgreSQL, Valkey and cookie sessions."""

import asyncio
from uuid import uuid4

import pytest
from chart_fixtures import seed_source
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from tinypods_api.main import create_app
from tinypods_core.applications import Application
from tinypods_core.applications.service import DefinitionService
from tinypods_core.audit import AuditRecord
from tinypods_core.auth import Principal
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.db.models import User
from tinypods_core.settings import Settings

BASE = "/api/v1"
DIGEST = "sha256:" + "a" * 64


def test_application_resources_and_memberships(auth_settings: Settings) -> None:
    """Exercise CRUD, filtering, conflicts, safe validation and membership protection.

    Args:
        auth_settings: Disposable migrated PostgreSQL and isolated Valkey namespace.
    """

    async def initialize() -> None:
        """Create only the disposable installation owner."""
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    app = create_app(auth_settings)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(BASE + "/applications").status_code == 401
        assert (
            client.post(
                BASE + "/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": client.cookies["__Host-tinypods-csrf"]}

        def post(path: str, payload: dict[str, object]) -> object:
            """Create a resource using the real authenticated CSRF boundary."""
            response = client.post(BASE + path, json=payload, headers=headers)
            assert response.status_code == 201, response.text
            return response.json()

        category = post("/categories", {"name": "Tools", "slug": "tools", "icon": "tools.svg"})
        application = post(
            "/applications", {"name": "Example", "slug": "example", "status": "stable"}
        )
        other = post("/applications", {"name": "Another", "slug": "another"})
        app_id = application["id"]
        release_body = {
            "application_id": app_id,
            "configuration_fields": [
                {"label": "Replicas", "type": "number", "path": "replicas", "value": 2}
            ],
            "chart_version_id": seed_source(
                auth_settings,
                version="1.0",
                source_location="https://charts.example.test",
                chart_name="example",
                digest=DIGEST,
            ),
        }
        release = post("/releases", release_body)
        for resource, data in (
            ("categories", category),
            ("applications", application),
            ("releases", release),
        ):
            path = "/" + resource + "/" + data["id"]
            assert client.get(BASE + path).status_code == 200
            assert client.get(BASE + path).json()["created"]
            assert client.get(BASE + "/" + resource + "?limit=1&offset=0").json()["limit"] == 1
            assert (
                client.post(
                    BASE + "/" + resource, json={"unknown": "sensitive"}, headers=headers
                ).status_code
                == 422
            )
            assert client.get(BASE + "/" + resource + "/" + str(uuid4())).status_code == 404
            assert (
                client.get(BASE + "/" + resource + "?ordering=untrusted_column").status_code == 422
            )
            assert client.get(BASE + "/" + resource + "?unexpected=1").status_code == 422
            assert client.patch(BASE + path, json={}, headers=headers).status_code == 200
        assert client.get(BASE + "/applications?status=stable").json()["total"] == 1
        assert (
            client.get(BASE + "/applications?slug=another").json()["items"][0]["id"] == other["id"]
        )
        assert client.get(BASE + "/applications?q=AMP").json()["total"] == 1
        assert client.get(BASE + "/applications?q=%25").json()["total"] == 0
        assert client.get(BASE + "/applications?ordering=-name").json()["items"][0]["id"] == app_id
        assert client.get(BASE + "/categories?slug=tools").json()["total"] == 1
        assert (
            client.get(
                BASE
                + "/releases?application_id="
                + app_id
                + "&source_type=helm_repository&q=example&ordering=-chart_version"
            ).json()["total"]
            == 1
        )
        assert client.get(BASE + "/releases?ordering=chart_version").status_code == 200
        assert client.get(BASE + "/applications?offset=100").json()["items"] == []
        assert (
            client.post(
                BASE + "/applications",
                json={"name": "duplicate", "slug": "example"},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.post(BASE + "/releases", json=release_body, headers=headers).status_code == 409
        )
        assert (
            client.post(
                BASE + "/releases",
                json={**release_body, "application_id": str(uuid4())},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.patch(
                BASE + "/applications/" + app_id,
                json={"description": "Changed", "status": "premium"},
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.get(BASE + "/releases/" + release["id"]).json()["chart_version_id"]
            == release_body["chart_version_id"]
        )
        assert (
            client.patch(
                BASE + "/applications/" + app_id, json={"name": None}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.patch(
                BASE + "/categories/" + category["id"], json={"icon": None}, headers=headers
            ).json()["icon"]
            is None
        )
        bad = {
            **release_body,
            "configuration_fields": [
                {
                    "label": "Password",
                    "type": "password",
                    "path": "password",
                    "value": "never-return-this-secret",
                }
            ],
        }
        rejected = client.post(BASE + "/releases", json=bad, headers=headers)
        assert rejected.status_code == 422 and "never-return-this-secret" not in rejected.text
        for resource, identity, body in (
            ("categories", category["id"], {"name": "Renamed", "slug": "renamed"}),
            ("applications", app_id, {"name": "Replaced", "slug": "replaced"}),
            ("releases", release["id"], {**release_body}),
        ):
            assert (
                client.put(
                    BASE + "/" + resource + "/" + identity, json=body, headers=headers
                ).status_code
                == 200
            )
            assert (
                client.patch(
                    BASE + "/" + resource + "/" + identity, json=body, headers=headers
                ).status_code
                == 200
            )
        assert client.get(BASE + "/applications/" + app_id).json()["description"] == ""
        assert (
            client.patch(
                BASE + "/releases/" + str(uuid4()),
                json={"configuration_fields": []},
                headers=headers,
            ).status_code
            == 404
        )
        memberships = "/applications/" + app_id + "/categories"
        assert client.get(BASE + memberships).json()["total"] == 0
        post(memberships, {"category_id": category["id"]})
        assert client.get(BASE + memberships).json()["items"] == [
            {"application_id": app_id, "category_id": category["id"]}
        ]
        assert (
            client.post(
                BASE + memberships, json={"category_id": category["id"]}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                BASE + memberships, json={"category_id": str(uuid4())}, headers=headers
            ).status_code
            == 404
        )
        missing_app = "/applications/" + str(uuid4()) + "/categories"
        assert client.get(BASE + missing_app).status_code == 404
        assert (
            client.post(
                BASE + missing_app, json={"category_id": category["id"]}, headers=headers
            ).status_code
            == 404
        )
        assert client.delete(BASE + "/applications/" + app_id, headers=headers).status_code == 409
        assert (
            client.delete(BASE + "/categories/" + category["id"], headers=headers).status_code
            == 409
        )
        assert (
            client.delete(BASE + memberships + "/" + category["id"], headers=headers).status_code
            == 204
        )
        assert (
            client.delete(BASE + memberships + "/" + category["id"], headers=headers).status_code
            == 404
        )
        assert (
            client.delete(BASE + "/releases/" + release["id"], headers=headers).status_code == 204
        )
        assert client.delete(BASE + "/applications/" + app_id, headers=headers).status_code == 204
        assert (
            client.delete(BASE + "/categories/" + category["id"], headers=headers).status_code
            == 204
        )
        assert client.delete(BASE + "/applications/" + app_id, headers=headers).status_code == 404

    async def verify() -> None:
        """Confirm real audit history and service-level validation/authorization."""
        database = Database(auth_settings)
        try:
            async with database.session() as session:
                events = (await session.exec(select(AuditRecord))).scalars().all()
                assert any(
                    row.action == "delete" and row.changed_object_id == app_id for row in events
                )
                assert "never-return-this-secret" not in str(
                    [row.postchange_data for row in events]
                )

            class Validator:
                """Return a trusted non-browser test identity for service guards."""

                async def validate_principal(self, principal: Principal) -> Principal:
                    """Model credential-neutral validation."""
                    return principal

            principal = Principal(uuid4(), "test", True, "test")
            async with database.session() as session:
                service = DefinitionService(Application, session, AuthorizationService(Validator()))
                for arguments in (
                    {"ordering": "password"},
                    {"ordering": "name", "limit": 0},
                    {"ordering": "name", "offset": -1},
                    {"ordering": "name", "filters": {"password": "x"}},
                ):
                    with pytest.raises(ValueError):
                        await service.list(principal, **{"limit": 25, "offset": 0, **arguments})
                with pytest.raises(ValueError):
                    await service.save(principal, {"id": uuid4()})
        finally:
            await database.close()

    asyncio.run(verify())


def test_non_owner_cannot_mutate_shared_definitions(auth_settings: Settings) -> None:
    """Authenticate a real ordinary user and deny writes before record lookup."""

    async def initialize() -> tuple[str, str]:
        """Issue a real non-owner session without creating default credentials."""
        from tinypods_core.auth import SessionService

        database = Database(auth_settings)
        try:
            async with database.transaction() as session:
                user = User(username="reader")
                session.add(user)
            login = await SessionService(database).issue(user.id)
            return (login.token, login.csrf_token)
        finally:
            await database.close()

    token, csrf = asyncio.run(initialize())
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        client.cookies.set("__Host-tinypods-session", token)
        headers = {"x-csrf-token": csrf}
        assert client.get(BASE + "/applications").status_code == 200
        assert (
            client.post(
                BASE + "/applications", json={"name": "Denied", "slug": "denied"}, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.patch(
                BASE + "/categories/" + str(uuid4()), json={"name": "Denied"}, headers=headers
            ).status_code
            == 403
        )
        assert client.delete(BASE + "/releases/" + str(uuid4()), headers=headers).status_code == 403
        assert (
            client.post(
                BASE + "/applications/" + str(uuid4()) + "/categories",
                json={"category_id": str(uuid4())},
                headers=headers,
            ).status_code
            == 403
        )


def test_definition_services_are_atomic_without_http(auth_settings: Settings) -> None:
    """Protect core service callers independently of HTTP authentication dependencies.

    Args:
        auth_settings: Disposable migrated database for isolated service transactions.
    """
    from tinypods_api.applications.contracts import ApplicationQuery, CategoryQuery, ReleaseQuery
    from tinypods_api.applications.routes import (
        list_applications,
        list_categories,
        list_memberships,
        list_releases,
    )
    from tinypods_api.http import PageParams
    from tinypods_core.applications import Category, Release
    from tinypods_core.applications.service import DefinitionError, MembershipService
    from tinypods_core.auth import AuthError

    async def scenario() -> None:
        """Commit each write, verify conflict rollback and inspect typed page serialization."""

        class Validator:
            """Revalidate a trusted non-browser credential supplied by an internal caller."""

            async def validate_principal(self, principal: Principal) -> Principal:
                """Return the test credential's normalized identity."""
                return principal

        authorization = AuthorizationService(Validator())
        owner = Principal(uuid4(), "service-owner", True, "opaque", credential_kind="test")
        reader = Principal(uuid4(), "service-reader", False, "opaque", credential_kind="test")
        database = Database(auth_settings)
        try:
            async with database.session() as session:
                application = await DefinitionService(Application, session, authorization).save(
                    owner, {"name": "Direct", "slug": "direct"}
                )
            async with database.session() as session:
                category = await DefinitionService(Category, session, authorization).save(
                    owner, {"name": "Group", "slug": "group"}
                )
            async with database.session() as session:
                release = await DefinitionService(Release, session, authorization).save(
                    owner,
                    {
                        "application_id": application.id,
                        "chart_version_id": seed_source(
                            auth_settings,
                            version="1",
                            source_location="oci://example.test/charts",
                            source_type="oci_registry",
                            chart_name="direct",
                            digest=DIGEST,
                        ),
                    },
                )
            for model, identity, handler, query in (
                (Application, application.id, list_applications, ApplicationQuery()),
                (Category, category.id, list_categories, CategoryQuery()),
                (Release, release.id, list_releases, ReleaseQuery()),
            ):
                async with database.session() as session:
                    service = DefinitionService(model, session, authorization)
                    assert (await service.get(reader, identity)).id == identity
                    with pytest.raises(DefinitionError) as absent:
                        await service.get(reader, uuid4())
                    assert absent.value.status == 404
                    page = await handler(query, service, reader)
                    assert page.total == 1 and page.items[0].id == identity
                async with database.session() as session:
                    with pytest.raises(AuthError) as denied:
                        await DefinitionService(model, session, authorization).delete(
                            reader, identity
                        )
                    assert denied.value.status == 403
            async with database.session() as session:
                with pytest.raises(DefinitionError) as duplicate:
                    await DefinitionService(Application, session, authorization).save(
                        owner, {"name": "Duplicate", "slug": "direct"}
                    )
                assert duplicate.value.status == 409
                assert not session.in_transaction()
            async with database.session() as session:
                changed = await DefinitionService(Application, session, authorization).save(
                    owner, {"description": "service edit"}, identity=application.id
                )
                assert changed.description == "service edit"
            async with database.session() as session:
                with pytest.raises(DefinitionError) as protected:
                    await DefinitionService(Application, session, authorization).delete(
                        owner, application.id
                    )
                assert protected.value.status == 409 and (not session.in_transaction())
            async with database.session() as session:
                pair = await MembershipService(session, authorization).add(
                    owner, application.id, category.id
                )
                assert pair.category_id == category.id
            async with database.session() as session:
                page = await list_memberships(
                    application.id, PageParams(), MembershipService(session, authorization), reader
                )
                assert page.total == 1 and page.items[0].category_id == category.id
                for limit, offset in ((0, 0), (101, 0), (25, -1)):
                    with pytest.raises(ValueError):
                        await MembershipService(session, authorization).list(
                            reader, application.id, limit=limit, offset=offset
                        )
            async with database.session() as session:
                with pytest.raises(DefinitionError) as duplicate:
                    await MembershipService(session, authorization).add(
                        owner, application.id, category.id
                    )
                assert duplicate.value.status == 409
            async with database.session() as session:
                with pytest.raises(DefinitionError) as absent:
                    await MembershipService(session, authorization).list(
                        reader, uuid4(), limit=25, offset=0
                    )
                assert absent.value.status == 404
            async with database.session() as session:
                await MembershipService(session, authorization).delete(
                    owner, application.id, category.id
                )
            async with database.session() as session:
                with pytest.raises(DefinitionError) as absent:
                    await MembershipService(session, authorization).delete(
                        owner, application.id, category.id
                    )
                assert absent.value.status == 404
            for app_id, category_id in ((uuid4(), category.id), (application.id, uuid4())):
                async with database.session() as session:
                    with pytest.raises(DefinitionError) as absent:
                        await MembershipService(session, authorization).add(
                            owner, app_id, category_id
                        )
                    assert absent.value.status == 404 and (not session.in_transaction())
            for model, identity in (
                (Release, release.id),
                (Application, application.id),
                (Category, category.id),
            ):
                async with database.session() as session:
                    await DefinitionService(model, session, authorization).delete(owner, identity)
        finally:
            await database.close()

    asyncio.run(scenario())
