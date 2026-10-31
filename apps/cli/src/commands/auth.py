"""Operator-only interactive initialization; credentials never enter command arguments."""

import asyncio
from uuid import UUID

import typer
from pydantic import SecretStr, ValidationError
from tinypods_cli.runtime import command_settings
from tinypods_core.auth import AuthError, Credentials, LocalPasswordProvider
from tinypods_core.auth.federation import IdentityMappings
from tinypods_core.db import Database

auth_app = typer.Typer(help="Manage authentication and identity links.", add_completion=False)


@auth_app.command()
def bootstrap() -> None:
    """Create the installation owner once after migrations, using hidden password prompts."""
    settings = command_settings()
    username = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        credentials = Credentials(username=username, password=SecretStr(password))
    except ValidationError:
        raise typer.BadParameter(
            "Use a 1-64 character ASCII account name (letters, digits, _.-) "
            "and a 15-128 character password"
        ) from None

    async def initialize() -> None:
        """Own the database lifecycle even if bootstrap is rejected or cancelled."""
        database = Database(settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials)
        finally:
            await database.close()

    try:
        asyncio.run(initialize())
    except AuthError as exc:
        raise typer.BadParameter(f"Owner bootstrap unavailable ({exc.status})") from None
    typer.echo("Installation owner created")


@auth_app.command()
def link(provider: str, subject: str, user_id: UUID) -> None:
    """Explicitly link an admitted provider subject to an existing core user as an operator.

    This can give an external identity the existing owner's rights, so it is never
    inferred from email/roles or exposed as an unauthenticated HTTP operation.
    """
    settings = command_settings()

    async def connect() -> None:
        """Own the database lifecycle around the explicit operator mapping."""
        database = Database(settings)
        try:
            await IdentityMappings(database, settings, provider=provider).link(subject, user_id)
        finally:
            await database.close()

    try:
        asyncio.run(connect())
    except AuthError as exc:
        raise typer.BadParameter(f"Identity linking unavailable ({exc.status})") from None
    typer.echo("External identity linked")
