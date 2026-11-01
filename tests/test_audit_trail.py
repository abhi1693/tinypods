"""Transactional history, safe authentication integration and isolated contexts."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from conftest import PostgresEndpoints
from pydantic import SecretStr
from sqlalchemy import delete, text, update
from sqlmodel import Session, select
from tinypods_core.audit import (
    AuditRecord,
    ModelRegistration,
    ObjectReference,
    ObjectType,
    audit_context,
    bind_user,
    register,
    sync_object_types,
)
from tinypods_core.audit.context import current_context
from tinypods_core.audit.registry import registry, safe_value
from tinypods_core.db import Database, migration_engine
from tinypods_core.db.migrations import MigrationManager
from tinypods_core.db.models import (
    AuthSession,
    ExternalIdentity,
    Installation,
    LocalCredential,
    LoginTransaction,
    ProviderGrant,
    ProviderLogoutEvent,
    RefreshCredential,
    User,
)
from tinypods_core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def audit_settings(postgres_endpoints: PostgresEndpoints) -> Settings:
    """Reset and migrate only disposable test PostgreSQL.

    Args:
        postgres_endpoints: Random isolated database endpoints.

    Returns:
        Migrated disposable connection configuration.
    """
    with psycopg.connect(postgres_endpoints.direct, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    settings = Settings(
        database_url=SecretStr(postgres_endpoints.direct),
        migration_database_url=SecretStr(postgres_endpoints.direct),
        database_mode="direct",
    )
    MigrationManager(settings, ROOT / "alembic.ini").upgrade()
    return settings


def test_crud_consolidation_savepoints_and_rollback(audit_settings: Settings) -> None:
    """Flushes and savepoints preserve one correct outer-transaction change.

    Args:
        audit_settings: Migrated disposable database.
    """

    async def scenario() -> None:
        """Own async resources and inspect finalized transaction records."""
        database = Database(audit_settings)
        try:
            with audit_context("system:worker") as context:
                async with database.transaction() as session:
                    user = User(username="first")
                    session.add(user)
                    await session.flush()
                    user.username = "second"
                    await session.flush()
                    with pytest.raises(RuntimeError):
                        async with session.begin_nested():
                            user.username = "rolled-back"
                            await session.flush()
                            raise RuntimeError("rollback savepoint")
                    await session.refresh(user)
                    assert user.username == "second"
                    async with session.begin_nested():
                        user.username = "final"
                        await session.flush()
                async with database.session() as session:
                    event = (await session.exec(select(AuditRecord))).one()
                    assert event.action == "create" and event.prechange_data is None
                    assert event.postchange_data["username"] == "final"
                    assert event.request_id == context.request_id
                    assert event.user_name == "system:worker"
                    assert event.related_object is None
                    assert (await event.changed_object.resolve(session)).id == user.id
                    assert await ObjectReference(uuid4(), str(user.id)).resolve(session) is None
            with audit_context("untrusted-snapshot", user_id=user.id):
                async with database.transaction() as session:
                    target = await session.get(User, user.id)
                    target.snapshot()
                    assert target.to_audit_record("update").prechange_data["username"] == "final"
                    target.username = "changed"
                    await session.flush()
                    target.username = "changed-again"
                    await session.flush()
                async with database.session() as session:
                    event = (
                        await session.exec(
                            select(AuditRecord).where(AuditRecord.action == "update")
                        )
                    ).one()
                    assert event.prechange_data["username"] == "final"
                    assert event.postchange_data["username"] == "changed-again"
                    assert event.user_name == "changed-again"
            async with database.transaction() as session:
                target = await session.get(User, user.id)
                target.last_updated = datetime.now(UTC)
                await session.flush()
                target.username = "temporary"
                await session.flush()
                target.username = "changed-again"
            with pytest.raises(RuntimeError):
                async with database.transaction() as session:
                    target = await session.get(User, user.id)
                    target.username = "abort"
                    await session.flush()
                    raise RuntimeError("abort outer")
            async with database.transaction() as session:
                temporary = User(username="create-delete")
                session.add(temporary)
                await session.flush()
                await session.delete(temporary)
            with audit_context("changed-again", user_id=user.id):
                async with database.transaction() as session:
                    target = await session.get(User, user.id)
                    await session.delete(target)
            async with database.session() as session:
                events = (await session.exec(select(AuditRecord).order_by(AuditRecord.time))).all()
                assert [event.action for event in events] == ["create", "update", "delete"]
                assert events[1].user_id is None and events[1].user_name == "changed-again"
                assert events[-1].prechange_data["username"] == "changed-again"
                assert await events[-1].changed_object.resolve(session) is None
                assert "audit_staging" not in session.sync_session.info
        finally:
            await database.close()

    asyncio.run(scenario())


def test_auth_allowlists_bulk_guard_and_append_only(audit_settings: Settings) -> None:
    """Every auth model records safe identities; secrets never enter history.

    Args:
        audit_settings: Migrated disposable database.
    """
    engine = migration_engine(audit_settings)
    secret = "sensitive-sentinel-should-never-appear"
    now = datetime.now(UTC)
    try:
        with Session(engine) as session:
            user = User(username="owner")
            session.add(user)
            session.flush()
            grant = ProviderGrant(
                user_id=user.id,
                issuer=secret,
                client_id=secret,
                subject=secret,
                encrypted_state=secret,
                configuration_digest=secret,
                authenticated_at=now,
                expires_at=now + timedelta(hours=1),
            )
            session.add(grant)
            session.flush()
            session.add_all(
                [
                    LocalCredential(user_id=user.id, password_hash=secret),
                    AuthSession(
                        token_digest=secret,
                        csrf_digest=secret,
                        user_id=user.id,
                        external_subject=secret,
                        expires_at=now,
                    ),
                    ExternalIdentity(user_id=user.id, issuer=secret, subject=secret),
                    LoginTransaction(
                        state_digest=secret,
                        browser_digest=secret,
                        nonce=secret,
                        verifier=secret,
                        configuration_digest=secret,
                        return_to=secret,
                        expires_at=now,
                    ),
                    RefreshCredential(token_digest=secret, csrf_digest=secret, grant_id=grant.id),
                    ProviderLogoutEvent(
                        event_digest=secret,
                        issuer=secret,
                        client_id=secret,
                        issued_at=now,
                        expires_at=now,
                    ),
                ]
            )
            session.commit()
            events = session.exec(select(AuditRecord)).all()
            assert len(events) == 8
            assert secret not in json.dumps([event.model_dump(mode="json") for event in events])
            credential = session.get(LocalCredential, user.id)
            credential.password_hash = "replacement-secret"
            session.commit()
            updates = session.exec(select(AuditRecord).where(AuditRecord.action == "update")).all()
            assert len(updates) == 1 and updates[0].prechange_data == updates[0].postchange_data
            for statement in [
                update(User).values(username="bypass"),
                delete(User),
                update(User.__table__.alias("user_alias")).values(username="bypass"),
                select(
                    update(User).values(username="bypass").returning(User.id).cte("bulk_change")
                ),
                User.__table__.insert().values(id=uuid4(), username="bypass"),
            ]:
                with pytest.raises(ValueError, match="Bulk mutation"):
                    session.execute(statement)
            # Core foundation tables without an audit registration keep their normal
            # SQLAlchemy DML behavior; the guard is scoped to audited models.
            session.execute(update(Installation).values(installation_id=uuid4()))
            session.add(ObjectType(id=uuid4(), app_label="evil", model="unreviewed"))
            with pytest.raises(ValueError, match="controlled writes"):
                session.flush()
            session.rollback()
        for statement in [
            "DELETE FROM audit_records",
            "UPDATE audit_records SET user_name='tampered'",
            "UPDATE audit_records SET user_id=NULL",
        ]:
            with engine.begin() as connection, pytest.raises(psycopg.Error):
                try:
                    connection.execute(text(statement))
                except Exception as exc:
                    raise exc.orig from exc
    finally:
        engine.dispose()


def test_populated_upgrade_and_registry_history(postgres_endpoints: PostgresEndpoints) -> None:
    """Backfill existing auth rows safely and retain types missing from installed code.

    Args:
        postgres_endpoints: Disposable PostgreSQL.
    """
    with psycopg.connect(postgres_endpoints.direct, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    settings = Settings(
        database_url=SecretStr(postgres_endpoints.direct),
        migration_database_url=SecretStr(postgres_endpoints.direct),
        database_mode="direct",
    )
    manager = MigrationManager(settings, ROOT / "alembic.ini")
    command.upgrade(manager.config(), "0002_authentication")
    user_id = uuid4()
    with psycopg.connect(postgres_endpoints.direct) as connection:
        connection.execute(
            "INSERT INTO auth_user (id, username, is_owner) VALUES (%s, 'existing', false)",
            (user_id,),
        )
        connection.execute(
            "INSERT INTO auth_session "
            "(token_digest, user_id, csrf_digest, source_provider, expires_at) "
            "VALUES ('secret', %s, 'secret', 'local', now())",
            (user_id,),
        )
    manager.upgrade()
    manager.upgrade()
    manager.check()
    engine = migration_engine(settings)
    try:
        with engine.begin() as connection:
            sync_object_types(connection)
            assert len(connection.execute(select(ObjectType)).all()) == 8
            session_row = connection.execute(
                text("SELECT id, created, last_updated FROM auth_session")
            ).one()
            assert session_row.id is not None and session_row.created == session_row.last_updated
            historical_id = uuid4()
            connection.execute(
                ObjectType.__table__.insert().values(
                    id=historical_id, app_label="removed", model="OldModel"
                )
            )
        manager.upgrade()
        with engine.connect() as connection:
            assert (
                connection.scalar(select(ObjectType.id).where(ObjectType.id == historical_id))
                == historical_id
            )
            assert connection.scalar(text("SELECT count(*) FROM audit_records")) == 0
        command.downgrade(manager.config(), "0002_authentication")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT username FROM auth_user")) == "existing"
        manager.upgrade()
        manager.check()
    finally:
        engine.dispose()


def test_context_isolation_and_registration_validation() -> None:
    """Context scopes isolate concurrent tasks and registry accepts only reviewed fields."""

    async def scenario(name: str) -> None:
        """Exercise one isolated asynchronous system operation."""
        with audit_context(name) as context:
            await asyncio.sleep(0)
            assert current_context.get() == context
            bind_user(uuid4(), "verified")
            assert current_context.get().user_name == "verified"
        assert current_context.get() is None

    async def run() -> None:
        """Run overlapping contexts on one event loop."""
        await asyncio.gather(scenario("system:cli"), scenario("system:worker"))

    asyncio.run(run())
    bind_user(uuid4(), "ignored outside context")
    with pytest.raises(ValueError, match="already registered"):
        register(registry[User])
    with pytest.raises(ValueError):
        safe_value(object())


def test_large_transaction_coalesces_and_rolls_back(audit_settings: Settings) -> None:
    """More than 1000 changes remain atomic across bounded batches and repeated flushes.

    Args:
        audit_settings: Disposable PostgreSQL configuration.
    """

    async def scenario() -> None:
        """Verify staging stays transactional without a Python object-count ceiling."""
        database = Database(audit_settings)
        try:
            async with database.transaction() as session:
                users = [User(username=f"large-{index}") for index in range(1200)]
                session.add_all(users)
                await session.flush()
                for user in users:
                    user.username += "-final"
                await session.flush()
            async with database.session() as session:
                events = (await session.exec(select(AuditRecord))).all()
                assert len(events) == 1200
                assert all(event.action == "create" for event in events)
                assert all(event.postchange_data["username"].endswith("-final") for event in events)
                assert len({event.request_id for event in events}) == 1
            with pytest.raises(RuntimeError):
                async with database.transaction() as session:
                    session.add_all(User(username=f"abort-{index}") for index in range(1200))
                    await session.flush()
                    raise RuntimeError("abort all batches")
            async with database.session() as session:
                assert len((await session.exec(select(User))).all()) == 1200
                assert len((await session.exec(select(AuditRecord))).all()) == 1200
        finally:
            await database.close()

    asyncio.run(scenario())


def test_relationships_cascades_and_registry_validation(audit_settings: Settings) -> None:
    """Only declared loaded relationships are captured, including delete cascades.

    Args:
        audit_settings: Disposable database configuration.
    """
    from typing import ClassVar
    from uuid import UUID

    from sqlalchemy import MetaData
    from sqlmodel import Field, Relationship
    from tinypods_core.audit import AuditTrailMixin

    test_metadata = MetaData()

    class Parent(AuditTrailMixin, table=True):
        """Disposable registered parent with explicitly tracked members."""

        __tablename__ = "audit_test_parent"
        metadata: ClassVar[MetaData] = test_metadata
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        label: str
        children: list["Child"] = Relationship(
            back_populates="parent", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
        )

    class Child(AuditTrailMixin, table=True):
        """Disposable registered cascade child."""

        __tablename__ = "audit_test_child"
        metadata: ClassVar[MetaData] = test_metadata
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent_id: UUID | None = Field(default=None, foreign_key="audit_test_parent.id")
        label: str
        parent: Parent | None = Relationship(back_populates="children")

    engine = migration_engine(audit_settings)
    try:
        with pytest.raises(ValueError, match="scalar"):
            register(ModelRegistration(Parent, "tests", "Parent", ("missing",)))
        with pytest.raises(ValueError, match="relationship"):
            register(
                ModelRegistration(Parent, "tests", "Parent", ("id",), relationships=("missing",))
            )
        register(
            ModelRegistration(
                Parent, "tests", "Parent", ("id", "label"), relationships=("children",)
            )
        )
        with pytest.raises(ValueError, match="label"):
            register(ModelRegistration(Child, "auth", "User", ("id",)))
        register(
            ModelRegistration(
                Child, "tests", "Child", ("id", "parent_id", "label"), related=("parent_id", Parent)
            )
        )
        child_spec = registry[Child]
        scalar_spec = ModelRegistration(
            Child, "tests", "Child", child_spec.fields, relationships=("parent",)
        )
        orphan = Child(label="orphan", parent=None)
        assert scalar_spec.serialize(orphan)["parent"] == []
        assert orphan.to_audit_record("create").related_object is None
        linked = Child(label="linked", parent=Parent(label="linked-parent", children=[]))
        assert len(scalar_spec.serialize(linked)["parent"]) == 1
        wide = Parent(label="safe" * 5000, children=[Child(label="member") for _ in range(105)])
        assert len(wide.serialize_object()["children"]) == 105
        assert len(wide.serialize_object()["label"]) == 20000
        test_metadata.create_all(engine)
        with engine.begin() as connection:
            sync_object_types(connection)
        with Session(engine) as session:
            parent = Parent(label="parent", children=[Child(label="child")])
            session.add(parent)
            session.commit()
            parent_id = parent.id
            child_event = session.exec(
                select(AuditRecord).where(
                    AuditRecord.changed_object_type_id == registry[Child].type_id
                )
            ).one()
            assert child_event.related_object.type_id == registry[Parent].type_id
            assert child_event.related_object.object_id == str(parent_id)
            assert child_event.postchange_data["parent_id"] == str(parent_id)
        with Session(engine) as session:
            parent = session.get(Parent, parent_id)
            parent.label = "unloaded"
            with pytest.raises(ValueError, match="Load declared"):
                session.flush()
            session.rollback()
            parent = session.get(Parent, parent_id)
            assert len(parent.children) == 1
            parent.children.append(Child(label="second"))
            session.commit()
            event = session.exec(select(AuditRecord).where(AuditRecord.action == "update")).one()
            assert len(event.prechange_data["children"]) == 1
            assert len(event.postchange_data["children"]) == 2
            assert len(parent.children) == 2
            session.delete(parent)
            session.commit()
            deletes = session.exec(select(AuditRecord).where(AuditRecord.action == "delete")).all()
            assert len(deletes) == 3
            session.add(Child(label="persisted-orphan", parent=None))
            session.commit()
        # Historical labels and records survive removal of installed model support.
        registry.pop(Parent)
        registry.pop(Child)
        with Session(engine) as session:
            assert len(session.exec(select(ObjectType)).all()) == 10
    finally:
        registry.pop(Parent, None)
        registry.pop(Child, None)
        test_metadata.drop_all(engine)
        engine.dispose()


def test_capture_guards_and_audit_failure_are_atomic(audit_settings: Settings) -> None:
    """Unsupported writes and history insert failure cannot commit business changes.

    Args:
        audit_settings: Disposable PostgreSQL.
    """
    from sqlalchemy.exc import DBAPIError

    async def scenario() -> None:
        """Exercise runtime session guards and injected audit storage failure."""
        database = Database(audit_settings)
        try:
            async with database.transaction() as session:
                user = User(username="safe")
                session.add(user)
            for method in ["bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"]:
                async with database.session() as session:
                    with pytest.raises(ValueError, match="Legacy bulk"):
                        if method == "bulk_save_objects":
                            await session.run_sync(
                                lambda sync: sync.bulk_save_objects([User(username="bad")])
                            )
                        else:
                            await session.run_sync(
                                lambda sync, method=method: getattr(sync, method)(User, [])
                            )
            async with database.session() as session:
                target = await session.get(User, user.id)
                target.id = uuid4()
                with pytest.raises(ValueError, match="identities"):
                    await session.flush()
            async with database.session() as session:
                target = User(username="partial")
                session.add(target)
                with pytest.raises(ValueError, match="Partial flush"):
                    await session.flush([target])
            engine = migration_engine(audit_settings)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE FUNCTION fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                            "BEGIN RAISE EXCEPTION 'injected audit failure'; END $$"
                        )
                    )
                    connection.execute(
                        text(
                            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_records "
                            "FOR EACH ROW EXECUTE FUNCTION fail_audit()"
                        )
                    )
                with pytest.raises(DBAPIError, match="injected audit failure"):
                    async with database.transaction() as session:
                        session.add(User(username="must-rollback"))
                async with database.session() as session:
                    assert [user.username for user in (await session.exec(select(User))).all()] == [
                        "safe"
                    ]
                    assert len((await session.exec(select(AuditRecord))).all()) == 1
            finally:
                engine.dispose()
        finally:
            await database.close()

    asyncio.run(scenario())


def test_first_savepoint_rollback_and_identity_reuse(audit_settings: Settings) -> None:
    """Rolled-back staging DDL is recreated and identity reuse produces ordered net state.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """

    async def scenario() -> None:
        """Exercise empty outer transactions and deletion followed by recreation."""
        database = Database(audit_settings)
        try:
            async with database.transaction() as session:
                with pytest.raises(RuntimeError):
                    async with session.begin_nested():
                        session.add(User(username="savepoint-only"))
                        await session.flush()
                        raise RuntimeError("abort first flush")
            async with database.session() as session:
                assert (await session.exec(select(AuditRecord))).all() == []
            async with database.transaction() as session:
                user = User(username="original")
                session.add(user)
            async with database.transaction() as session:
                target = await session.get(User, user.id)
                await session.delete(target)
                await session.flush()
                session.add(User(id=user.id, username="replacement"))
            async with database.session() as session:
                event = (
                    await session.exec(select(AuditRecord).where(AuditRecord.action == "update"))
                ).one()
                assert event.prechange_data["username"] == "original"
                assert event.postchange_data["username"] == "replacement"
                assert (
                    await ObjectReference(registry[User].type_id, "invalid-uuid").resolve(session)
                    is None
                )
            transient = User(username="manual")
            transient.snapshot()
            assert transient.to_audit_record("create").prechange_data is None
            assert transient.to_audit_record("delete").postchange_data is None
            assert (
                User(username="without-snapshot").to_audit_record("update").prechange_data
                is not None
            )
        finally:
            await database.close()

    asyncio.run(scenario())


def test_http_actor_and_request_correlation(audit_settings: Settings) -> None:
    """Verified request actors and generated request UUIDs reach committed history.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """
    from fastapi import Request
    from fastapi.testclient import TestClient
    from tinypods_api.auth.dependencies import PrincipalDependency
    from tinypods_api.main import create_app
    from tinypods_core.auth import SessionService

    async def prepare() -> object:
        """Create one real user and a valid session outside the HTTP request."""
        database = Database(audit_settings)
        try:
            async with database.transaction() as session:
                user = User(username="http-owner")
                session.add(user)
            return await SessionService(database).issue(user.id)
        finally:
            await database.close()

    login = asyncio.run(prepare())
    app = create_app(audit_settings)

    @app.post("/audit-write")
    async def write(request: Request, principal: PrincipalDependency) -> dict[str, bool]:
        """Exercise a verified authenticated write through real API middleware."""
        async with request.app.state.database.transaction() as session:
            user = await session.get(User, principal.user_id)
            user.is_owner = True
        return {"ok": True}

    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set("__Host-tinypods-session", login.token)
        client.cookies.set("__Host-tinypods-csrf", login.csrf_token)
        response = client.post(
            "/audit-write", headers={"x-csrf-token": login.csrf_token, "x-request-id": "untrusted"}
        )
        assert response.status_code == 200
    engine = migration_engine(audit_settings)
    try:
        with Session(engine) as session:
            event = session.exec(select(AuditRecord).where(AuditRecord.action == "update")).one()
            assert str(event.request_id) == response.headers["x-request-id"]
            assert event.user_id == login.principal.user_id
            assert event.user_name == "http-owner"
    finally:
        engine.dispose()


def test_related_reference_resolves_and_preserves_actor(audit_settings: Settings) -> None:
    """A real auth credential references the safe user identity without credential data.

    Args:
        audit_settings: Disposable migrated PostgreSQL.
    """

    async def scenario() -> None:
        """Resolve a related object through the explicit async registry boundary."""
        database = Database(audit_settings)
        try:
            async with database.transaction() as session:
                user = User(username="related-owner")
                session.add(user)
            with audit_context(user.username, user_id=user.id):
                async with database.transaction() as session:
                    session.add(LocalCredential(user_id=user.id, password_hash="never-copy"))
            async with database.session() as session:
                event = (
                    await session.exec(
                        select(AuditRecord).where(
                            AuditRecord.changed_object_type_id == registry[LocalCredential].type_id
                        )
                    )
                ).one()
                related = await event.related_object.resolve(session)
                assert related.id == user.id
                assert event.user_name == user.username
        finally:
            await database.close()

    asyncio.run(scenario())


def test_stale_loaded_fields_do_not_distort_snapshots(audit_settings: Settings) -> None:
    """Before/after snapshots reflect persisted rows, including concurrently changed fields.

    Args:
        audit_settings: Disposable PostgreSQL.
    """

    async def scenario() -> None:
        """Load two snapshots, then commit disjoint changes through separate sessions."""
        database = Database(audit_settings)
        try:
            async with database.transaction() as session:
                user = User(username="original")
                session.add(user)
            async with database.session() as first, database.session() as stale:
                one = await first.get(User, user.id)
                two = await stale.get(User, user.id)
                one.username = "persisted"
                await first.commit()
                assert two.username == "original"
                two.is_owner = True
                await stale.commit()
            async with database.session() as session:
                events = (
                    await session.exec(
                        select(AuditRecord)
                        .where(AuditRecord.action == "update")
                        .order_by(AuditRecord.time)
                    )
                ).all()
                assert len(events) == 2
                assert events[-1].prechange_data["username"] == "persisted"
                assert events[-1].postchange_data["username"] == "persisted"
                assert events[-1].prechange_data["is_owner"] is False
                assert events[-1].postchange_data["is_owner"] is True
        finally:
            await database.close()

    asyncio.run(scenario())
