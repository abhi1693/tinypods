"""Typed CLI commands with shared logging and safe process-level failures."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError
from tinypods_core.db import Database
from tinypods_core.log_constants import LogEvent
from tinypods_core.logging import configure_logging, failure_fields, log_context, shutdown_logging
from tinypods_core.migrations import MigrationManager
from tinypods_core.settings import ConfigurationError, Settings, load_settings

logger = logging.getLogger("tinypods.cli")
app = typer.Typer(help="TinyPods CLI", add_completion=False, pretty_exceptions_enable=False)
database_app = typer.Typer(help="Manage core migrations explicitly.", add_completion=False)
app.add_typer(database_app, name="db")
ConfigOption = Annotated[Path, typer.Option("--config", help="Alembic configuration path.")]


def command_settings() -> Settings:
    """Validate configuration and initialize logging for the current CLI command.

    Returns:
        Validated deployment settings.
    """
    settings = load_settings()
    configure_logging(settings, service="cli")
    logger.info(LogEvent.COMMAND_STARTED)
    return settings


async def check_database(settings: Settings) -> str:
    """Read installation identity and always dispose the temporary async engine.

    Args:
        settings: Validated runtime settings controlling the resource configuration.

    Returns:
        JSON containing the installation identifier and creation time.
    """
    database = Database(settings)
    try:
        return (await database.installation()).model_dump_json()
    finally:
        await database.close()


@app.command()
def doctor(
    database: Annotated[
        bool, typer.Option("--database", help="Check the migrated database.")
    ] = False,
) -> None:
    """Validate configuration, optionally checking PostgreSQL.

    Args:
        database: Whether to check migrated database connectivity.
    """
    settings = command_settings()
    if database:
        typer.echo(asyncio.run(check_database(settings)))
    else:
        typer.echo(json.dumps({"configuration": "valid", "service": settings.service_name}))


@database_app.command()
def upgrade(config: ConfigOption = Path("alembic.ini")) -> None:
    """Apply pending core migrations.

    Args:
        config: Path to the Alembic configuration used by the command.
    """
    MigrationManager(command_settings(), config).upgrade()
    typer.echo(json.dumps({"migration": "upgraded"}))


@database_app.command()
def check(config: ConfigOption = Path("alembic.ini")) -> None:
    """Check whether core models need a new migration.

    Args:
        config: Path to the Alembic configuration used by the command.
    """
    MigrationManager(command_settings(), config).check()


@database_app.command()
def current(config: ConfigOption = Path("alembic.ini")) -> None:
    """Show applied core migration revisions.

    Args:
        config: Path to the Alembic configuration used by the command.
    """
    revisions = MigrationManager(command_settings(), config).current()
    typer.echo(json.dumps({"revisions": revisions}))


def main() -> None:
    """Run a correlated command with process-safe errors and logging cleanup."""
    with log_context(command_id=str(uuid4())):
        try:
            app()
        except SystemExit as exc:
            if exc.code in (0, None):
                logger.info(LogEvent.COMMAND_COMPLETED)
            raise
        except ConfigurationError as exc:
            raise SystemExit(str(exc)) from None
        except (SQLAlchemyError, CommandError) as exc:
            logger.error(LogEvent.COMMAND_FAILED, extra=failure_fields(exc))
            raise SystemExit(
                "Database operation failed; check connectivity and migration state"
            ) from None
        except Exception as exc:
            logger.error(LogEvent.COMMAND_FAILED, extra=failure_fields(exc))
            raise SystemExit("Command failed; check application logs") from None
        finally:
            shutdown_logging()
