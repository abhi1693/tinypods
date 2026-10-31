"""Database behavior against real, isolated PostgreSQL and transaction pooling."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from conftest import PostgresEndpoints
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlmodel import Field, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_api.http.dependencies import get_session
from tinypods_api.main import create_app
from tinypods_core.db import Database, database_url, migration_engine
from tinypods_core.db.migrations import MIGRATION_LOCK, MigrationManager
from tinypods_core.db.models import Installation, core_metadata
from tinypods_core.settings import ConfigurationError, Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(postgres_endpoints: PostgresEndpoints) -> Settings:
    """Reset the disposable public schema and return its direct connection settings.

    Args:
        postgres_endpoints: Disposable direct PostgreSQL and transaction-pool endpoints.

    Returns:
        Validated settings for the disposable database.
    """
    # This connection is always our disposable fixture, never a caller-provided DSN.
    with psycopg.connect(postgres_endpoints.direct, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    return Settings(database_url=SecretStr(postgres_endpoints.direct))


def manager(settings: Settings) -> MigrationManager:
    """Create a migration manager for the isolated database and repository configuration.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.

    Returns:
        Manager bound to the disposable database and core Alembic configuration.
    """
    return MigrationManager(settings, ROOT / "alembic.ini")


@pytest.mark.parametrize("mode", ["direct", "pgbouncer"])
def test_sessions_commit_rollback_and_pooling(
    settings: Settings,
    postgres_endpoints: PostgresEndpoints,
    mode: str,
) -> None:
    """Verify sessions commit rollback and pooling.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
        postgres_endpoints: Disposable direct PostgreSQL and transaction-pool endpoints.
        mode: Parameterized direct, pooled or malformed-contract scenario.
    """
    manager(settings).upgrade()
    configured = (
        settings
        if mode == "direct"
        else Settings(
            database_url=SecretStr(postgres_endpoints.pooled),
            database_mode="pgbouncer",
            migration_database_url=settings.database_url,
        )
    )

    async def scenario() -> None:
        """Run the async lifecycle for sessions commit rollback and pooling."""
        database = Database(configured)
        try:
            assert isinstance(database.engine.pool, NullPool) is (mode == "pgbouncer")
            original = (await database.installation()).installation_id
            changed = uuid4()
            async with database.transaction() as session:
                record = await session.get(Installation, 1)
                assert record is not None
                record.installation_id = changed
            assert (await database.installation()).installation_id == changed
            with pytest.raises(RuntimeError, match="abort"):
                async with database.transaction() as session:
                    await session.exec(
                        text("UPDATE platform_installation SET installation_id=:id"),
                        params={"id": original},
                    )
                    raise RuntimeError("abort")
            assert (await database.installation()).installation_id == changed
            async with database.session() as session:
                await session.exec(
                    text("UPDATE platform_installation SET installation_id=:id"),
                    params={"id": original},
                )
            assert (await database.installation()).installation_id == changed
            async with database.session() as one, database.session() as two:
                assert one is not two
            for _ in range(12):
                assert await database.ping()
            assert set((await database.installation()).model_dump()) == {
                "installation_id",
                "created_at",
            }
        finally:
            await database.close()

    asyncio.run(scenario())


def test_migrations_preserve_identity_and_unmanaged_tables(settings: Settings) -> None:
    """Verify migrations preserve identity and unmanaged tables.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
    """
    migration = manager(settings)
    assert migration.current() == ()
    command.upgrade(migration.config(), "0001_installation")
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        original = connection.execute(
            "SELECT installation_id FROM platform_installation"
        ).fetchone()
        connection.execute("CREATE TABLE plugin_data (id integer PRIMARY KEY, note text)")
        connection.execute("INSERT INTO plugin_data VALUES (1, 'keep me')")

    class PluginRecord(SQLModel, table=True):
        """Represent an unmanaged extension table that core migrations must preserve."""

        __tablename__ = "plugin_data"
        id: int = Field(primary_key=True)
        note: str

    assert "plugin_data" not in core_metadata.tables
    migration.upgrade()
    migration.check()
    assert migration.current() == ("0002_authentication",)
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert (
            connection.execute("SELECT installation_id FROM platform_installation").fetchone()
            == original
        )
        assert connection.execute("SELECT note FROM plugin_data").fetchone() == ("keep me",)
    command.downgrade(migration.config(), "base")
    assert migration.current() == ()
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert connection.execute("SELECT note FROM plugin_data").fetchone() == ("keep me",)


def test_populated_upgrade_and_failed_migration_are_transactional(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Verify populated upgrade and failed migration are transactional.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
        tmp_path: Pytest-owned temporary directory for disposable artifacts.
    """
    migration = manager(settings)
    migration.upgrade()
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        original = connection.execute(
            "SELECT installation_id FROM platform_installation"
        ).fetchone()
    shutil.copytree(ROOT / "migrations", tmp_path / "migrations")
    revision = tmp_path / "migrations/versions/0002_test.py"
    revision.write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision='0002_test'\ndown_revision='0002_authentication'\n"
        "def upgrade():\n    op.add_column('platform_installation', sa.Column('note', sa.Text()))\n"
        "def downgrade():\n    op.drop_column('platform_installation', 'note')\n"
    )
    config = migration.config()
    config.set_main_option("script_location", str(tmp_path / "migrations"))
    command.upgrade(config, "head")
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert (
            connection.execute("SELECT installation_id FROM platform_installation").fetchone()
            == original
        )
        connection.execute("UPDATE platform_installation SET note='survives'")
    revision = tmp_path / "migrations/versions/0003_failure.py"
    revision.write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision='0003_failure'\ndown_revision='0002_test'\n"
        "def upgrade():\n"
        "    op.add_column('platform_installation', sa.Column('broken', sa.Text()))\n"
        "    raise RuntimeError('migration failed')\n"
    )
    with pytest.raises(RuntimeError, match="migration failed"):
        command.upgrade(config, "head")
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert connection.execute(
            "SELECT version_num FROM tinypods_alembic_version"
        ).fetchone() == ("0002_test",)
        assert connection.execute("SELECT note FROM platform_installation").fetchone() == (
            "survives",
        )
        assert (
            connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='platform_installation' AND column_name='broken'"
            ).fetchone()
            is None
        )


def test_migration_lock_is_exclusive_and_released(settings: Settings) -> None:
    """Verify migration lock is exclusive and released.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
    """
    with psycopg.connect(settings.database_url.get_secret_value(), autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            with pytest.raises(ConfigurationError, match="already running"):
                manager(settings).upgrade()
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    manager(settings).upgrade()
    manager(settings).check()


def test_readiness_and_request_session_lifecycle(settings: Settings) -> None:
    """Verify readiness and request session lifecycle.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
    """
    app = create_app(settings)
    sessions: list[AsyncSession] = []

    @app.post("/test-write")
    async def write(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, bool]:
        """Exercise a write inside the enclosing transaction or request lifecycle scenario.

        Args:
            session: Request-owned asynchronous SQLModel session.

        Returns:
            Boolean fields returned by the synthetic endpoint.
        """
        sessions.append(session)
        await session.exec(
            text("UPDATE platform_installation SET installation_id=:id"), params={"id": uuid4()}
        )
        # No commit: dependency cleanup must roll this back.
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200
        manager(settings).upgrade()
        assert client.get("/readyz").status_code == 200
        with psycopg.connect(settings.database_url.get_secret_value()) as connection:
            before = connection.execute(
                "SELECT installation_id FROM platform_installation"
            ).fetchone()
        assert client.post("/test-write").status_code == 200
        assert client.post("/test-write").status_code == 200
        assert sessions[0] is not sessions[1]
        with psycopg.connect(settings.database_url.get_secret_value()) as connection:
            assert (
                connection.execute("SELECT installation_id FROM platform_installation").fetchone()
                == before
            )
    app = create_app(Settings())
    app.post("/test-write")(write)
    with TestClient(app) as client:
        assert client.post("/test-write").status_code == 503
    with pytest.raises(ValueError, match="reserved"):
        create_app(settings, readiness_checks={"database": lambda: asyncio.sleep(0)})


def test_database_wait_does_not_block_event_loop(settings: Settings) -> None:
    """Verify database wait does not block event loop.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
    """
    manager(settings).upgrade()

    async def scenario() -> None:
        """Run the async lifecycle for database wait does not block event loop."""
        database = Database(settings)
        completed = asyncio.Event()

        async def slow_query() -> None:
            """Run a server-side wait through the asynchronous SQLAlchemy session."""
            async with database.session() as session:
                await session.exec(text("SELECT pg_sleep(0.3)"))
            completed.set()

        try:
            task = asyncio.create_task(slow_query())
            await asyncio.sleep(0.05)
            assert not completed.is_set()
            await task
        finally:
            await database.close()

    asyncio.run(scenario())


def test_settings_and_migration_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify settings and migration configuration.

    Args:
        tmp_path: Pytest-owned temporary directory for disposable artifacts.
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    with pytest.raises(ConfigurationError, match="required"):
        database_url(Settings())
    pooled = Settings(
        database_url=SecretStr("postgresql://app@localhost/db"), database_mode="pgbouncer"
    )
    with pytest.raises(ConfigurationError, match="direct"):
        database_url(pooled, migrations=True)
    direct = SecretStr("postgresql://migration@localhost/db")
    configured = pooled.model_copy(update={"migration_database_url": direct})
    assert database_url(configured, migrations=True).username == "migration"
    engine = migration_engine(configured)
    assert isinstance(engine.pool, NullPool)
    engine.dispose()
    with pytest.raises(ConfigurationError, match="does not exist"):
        MigrationManager(Settings(), tmp_path / "missing.ini")
    for value in ["not-a-url", "sqlite:///db", "postgresql+asyncpg://host/db"]:
        with pytest.raises(ValidationError):
            Settings(database_url=SecretStr(value))
    monkeypatch.setenv("TINYPODS_DATABASE_URL", "invalid-private-secret")
    with pytest.raises(ConfigurationError) as error:
        load_settings()
    assert "invalid-private-secret" not in str(error.value)


def test_cli_database_commands(settings: Settings) -> None:
    """Verify cli database commands.

    Args:
        settings: Validated settings pointing only at isolated test infrastructure.
    """
    environment = dict(os.environ, TINYPODS_DATABASE_URL=settings.database_url.get_secret_value())
    for args in [["db", "upgrade"], ["db", "current"], ["db", "check"], ["doctor", "--database"]]:
        result = subprocess.run(
            ["tinypods", *args], env=environment, capture_output=True, text=True, timeout=20
        )
        assert result.returncode == 0, result.stderr
        assert settings.database_url.get_secret_value() not in result.stdout + result.stderr
        if args[0] == "doctor":
            assert "installation_id" in json.loads(result.stdout)
    result = subprocess.run(
        ["tinypods", "doctor", "--database"],
        env=dict(environment, TINYPODS_DATABASE_URL="postgresql://private-secret@127.0.0.1:1/db"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "private-secret" not in result.stdout + result.stderr
    result = subprocess.run(
        ["tinypods", "db", "upgrade", "--config", "missing.ini"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "does not exist" in result.stderr


@pytest.mark.parametrize("mode", ["direct", "pgbouncer"])
def test_cancelled_transaction_releases_connection(
    settings: Settings, postgres_endpoints: PostgresEndpoints, mode: str
) -> None:
    """Verify a cancelled write rolls back and releases the only available connection.

    Args:
        settings: Configuration for the disposable reset database.
        postgres_endpoints: Isolated direct and transaction-pool endpoints.
        mode: Connection strategy under test.
    """
    manager(settings).upgrade()
    configured = settings.model_copy(update={"database_pool_size": 1, "database_max_overflow": 0})
    if mode == "pgbouncer":
        configured = configured.model_copy(
            update={
                "database_url": SecretStr(postgres_endpoints.pooled),
                "database_mode": mode,
                "migration_database_url": settings.database_url,
            }
        )

    async def scenario() -> None:
        """Own database cleanup while cancelling a flushed transaction."""
        database = Database(configured)
        updated = asyncio.Event()
        try:
            original = (await database.installation()).installation_id

            async def write() -> None:
                """Flush a changed identity and expose the cancellation window."""
                async with database.transaction() as session:
                    record = await session.get(Installation, 1)
                    assert record is not None
                    record.installation_id = uuid4()
                    await session.flush()
                    updated.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(write())
            try:
                async with asyncio.timeout(5):
                    await updated.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert (await database.installation()).installation_id == original
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            await database.close()

    asyncio.run(scenario())
