"""Core migrations own only explicitly registered core tables."""

from alembic import context
from sqlalchemy import text
from tinypods_core.db import database_url, migration_engine
from tinypods_core.db.migrations import MIGRATION_LOCK
from tinypods_core.db.models import core_metadata
from tinypods_core.settings import ConfigurationError, load_settings

config = context.config
settings = config.attributes.get("settings") or load_settings()


def include_name(
    name: str | None,
    type_: str,
    parent_names: object,
) -> bool:
    if type_ == "table":
        return name in core_metadata.tables
    return True


if context.is_offline_mode():
    context.configure(
        url=database_url(settings, migrations=True),
        literal_binds=True,
        target_metadata=core_metadata,
        include_name=include_name,
        version_table="tinypods_alembic_version",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = migration_engine(settings)
    try:
        with engine.connect() as connection:
            acquired = connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK}
            )
            connection.commit()
            if not acquired:
                raise ConfigurationError("Another core migration is already running")
            try:
                context.configure(
                    connection=connection,
                    target_metadata=core_metadata,
                    include_name=include_name,
                    version_table="tinypods_alembic_version",
                    compare_type=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
            finally:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
                connection.commit()
    finally:
        engine.dispose()
