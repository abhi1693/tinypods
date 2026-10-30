"""Explicit migration operations using an operator-provided direct connection."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from tinypods_core.db import database_url, migration_engine
from tinypods_core.settings import ConfigurationError, Settings

MIGRATION_LOCK = 7464696


class MigrationManager:
    def __init__(self, settings: Settings, config_path: Path) -> None:
        self.settings = settings
        self.config_path = config_path.resolve()
        if not self.config_path.is_file():
            raise ConfigurationError("Alembic configuration file does not exist")

    def config(self) -> Config:
        config = Config(str(self.config_path))
        config.attributes["settings"] = self.settings
        # Validate the direct URL before Alembic starts, without placing a secret in INI text.
        database_url(self.settings, migrations=True)
        return config

    def upgrade(self) -> None:
        command.upgrade(self.config(), "head")

    def check(self) -> None:
        command.check(self.config())

    def current(self) -> tuple[str, ...]:
        engine = migration_engine(self.settings)
        try:
            with engine.connect() as connection:
                return MigrationContext.configure(
                    connection, opts={"version_table": "tinypods_alembic_version"}
                ).get_current_heads()
        finally:
            engine.dispose()
