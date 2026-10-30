"""Typed CLI commands for diagnostics and explicit database administration."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError
from tinypods_core.db import Database
from tinypods_core.migrations import MigrationManager
from tinypods_core.settings import ConfigurationError, Settings, load_settings

app = typer.Typer(help="TinyPods CLI", add_completion=False, pretty_exceptions_enable=False)
database_app = typer.Typer(help="Manage core migrations explicitly.", add_completion=False)
app.add_typer(database_app, name="db")

ConfigOption = Annotated[Path, typer.Option("--config", help="Alembic configuration path.")]


async def check_database(settings: Settings) -> str:
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
    """Validate configuration, optionally checking PostgreSQL."""
    settings = load_settings()
    if database:
        typer.echo(asyncio.run(check_database(settings)))
    else:
        typer.echo(json.dumps({"configuration": "valid", "service": settings.service_name}))


@database_app.command()
def upgrade(config: ConfigOption = Path("alembic.ini")) -> None:
    """Apply pending core migrations."""
    MigrationManager(load_settings(), config).upgrade()
    typer.echo(json.dumps({"migration": "upgraded"}))


@database_app.command()
def check(config: ConfigOption = Path("alembic.ini")) -> None:
    """Check whether core models need a new migration."""
    MigrationManager(load_settings(), config).check()


@database_app.command()
def current(config: ConfigOption = Path("alembic.ini")) -> None:
    """Show applied core migration revisions."""
    revisions = MigrationManager(load_settings(), config).current()
    typer.echo(json.dumps({"revisions": revisions}))


def main() -> None:
    try:
        app()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    except (SQLAlchemyError, CommandError):
        raise SystemExit(
            "Database operation failed; check connectivity and migration state"
        ) from None
