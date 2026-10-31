"""Explicit migration operations using an operator-provided direct connection."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from tinypods_core.db import database_url, migration_engine
from tinypods_core.settings import ConfigurationError, Settings

MIGRATION_LOCK = 7464696


class MigrationManager:
    """Run explicit synchronous core migrations through a direct PostgreSQL connection."""

    def __init__(self, settings: Settings, config_path: Path) -> None:
        """Retain validated settings and resolve an existing Alembic configuration file.

        Args:
            settings: Validated runtime settings controlling the resource configuration.
            config_path: Alembic configuration path, resolved before any migration work.
        """
        self.settings = settings
        self.config_path = config_path.resolve()
        if not self.config_path.is_file():
            raise ConfigurationError("Alembic configuration file does not exist")

    def config(self) -> Config:
        """Build Alembic configuration without placing secret connection values in INI text.

        Returns:
            Alembic configuration carrying validated settings in attributes.
        """
        config = Config(str(self.config_path))
        config.attributes["settings"] = self.settings
        # Validate the direct URL before Alembic starts, without placing a secret in INI text.
        database_url(self.settings, migrations=True)
        return config

    def upgrade(self) -> None:
        """Apply pending core migrations to the configured head revision."""
        command.upgrade(self.config(), "head")

    def check(self) -> None:
        """Detect core model changes requiring a new migration."""
        command.check(self.config())

    def current(self) -> tuple[str, ...]:
        """Read applied core revision heads and dispose the temporary migration engine.

        Returns:
            Applied core revision identifiers.
        """
        engine = migration_engine(self.settings)
        try:
            with engine.connect() as connection:
                return MigrationContext.configure(
                    connection, opts={"version_table": "tinypods_alembic_version"}
                ).get_current_heads()
        finally:
            engine.dispose()
