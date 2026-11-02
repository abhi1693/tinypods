"""Populated upgrades preserve existing encrypted cluster credentials byte for byte."""

import asyncio
from pathlib import Path

import psycopg
from alembic import command
from secret_fixtures import key_settings, kubeconfig
from test_auth import auth_settings as auth_settings
from test_cluster_secrets import cluster_records, owner, service
from tinypods_core.db import Database
from tinypods_core.db.migrations import MigrationManager
from tinypods_core.settings import Settings


def test_populated_ciphertext_upgrade(auth_settings: Settings) -> None:
    """Upgrade the previous schema without rewriting ciphertext, nonces or cluster AAD."""
    settings = key_settings(auth_settings)
    manager = MigrationManager(settings, Path(__file__).resolve().parents[1] / "alembic.ini")
    command.downgrade(manager.config(), "0008_release_publication")

    async def create() -> None:
        async with Database(settings) as database:
            cluster, _ = await cluster_records(database)
            async with database.session() as session:
                await service(session, settings).create(
                    owner(), cluster, "Existing", "", kubeconfig()
                )

    asyncio.run(create())
    query = (
        "SELECT id, secret_id, version, encryption_key_id, nonce, encrypted_payload "
        "FROM secret_versions"
    )
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        before = connection.execute(query).fetchall()
    manager.upgrade()
    manager.check()
    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        assert connection.execute(query).fetchall() == before

    async def resolve() -> None:
        from sqlmodel import select
        from tinypods_core.secrets.models import ClusterSecret

        async with Database(settings) as database, database.session() as session:
            current = service(session, settings)
            binding = (await session.exec(select(ClusterSecret))).one()
            assert binding.active_version_id is not None
            version = await current._payload_version(binding.active_version_id)
            assert await current._resolve(version, binding) == kubeconfig()

    asyncio.run(resolve())
