"""Explicit core migration commands and their Typer command group."""

import json
from pathlib import Path
from typing import Annotated

import typer
from tinypods_cli.runtime import command_settings
from tinypods_core.db.migrations import MigrationManager

database_app = typer.Typer(help="Manage core migrations explicitly.", add_completion=False)


ConfigOption = Annotated[Path, typer.Option("--config", help="Alembic configuration path.")]


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
