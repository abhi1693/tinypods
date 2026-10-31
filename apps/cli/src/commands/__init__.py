"""Public registration boundary for the CLI command groups."""

import typer
from tinypods_cli.commands.database import database_app
from tinypods_cli.commands.doctor import doctor

__all__ = ["register_commands"]


def register_commands(application: typer.Typer) -> None:
    """Register concrete commands without coupling them to the process entry point.

    Args:
        application: Typer application receiving diagnostics and migration commands.
    """
    application.command()(doctor)
    application.add_typer(database_app, name="db")
