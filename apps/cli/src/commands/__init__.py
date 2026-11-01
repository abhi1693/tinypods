"""Public registration boundary for the CLI command groups."""

import typer
from tinypods_cli.commands.audit import audit_app
from tinypods_cli.commands.auth import auth_app
from tinypods_cli.commands.database import database_app
from tinypods_cli.commands.doctor import doctor
from tinypods_cli.commands.repositories import repositories_app

__all__ = ["register_commands"]


def register_commands(application: typer.Typer) -> None:
    """Register concrete commands without coupling them to the process entry point.

    Args:
        application: Typer application receiving diagnostics and migration commands.
    """
    application.command()(doctor)
    application.add_typer(database_app, name="db")
    application.add_typer(auth_app, name="auth")
    application.add_typer(audit_app, name="audit")
    application.add_typer(repositories_app, name="repositories")
