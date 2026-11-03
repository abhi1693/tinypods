"""Operator-only interactive initialization; credentials never enter command arguments."""

import asyncio
import secrets
from uuid import UUID

import typer
from pydantic import SecretStr, ValidationError
from tinypods_cli.runtime import command_settings
from tinypods_core.auth import AuthError, Credentials, LocalPasswordProvider
from tinypods_core.auth.federation import IdentityMappings
from tinypods_core.auth.ownership import OwnerAccess
from tinypods_core.db import Database

auth_app = typer.Typer(help="Manage authentication and identity links.", add_completion=False)


@auth_app.command("setup-token")
def setup_token() -> None:
    """Generate a private setup secret for TINYPODS_SETUP_TOKEN configuration."""
    typer.echo(secrets.token_urlsafe(32))


@auth_app.command()
def bootstrap() -> None:
    """Create the instance owner once after migrations, using hidden password prompts."""
    settings = command_settings()
    username = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        credentials = Credentials(username=username, password=SecretStr(password))
    except ValidationError:
        raise typer.BadParameter(
            "Use a valid username or email address and a 15-128 character password"
        ) from None

    async def initialize() -> None:
        """Own the database lifecycle even if bootstrap is rejected or cancelled."""
        async with Database(settings) as database:
            await LocalPasswordProvider(database).bootstrap(credentials)

    try:
        asyncio.run(initialize())
    except AuthError as exc:
        raise typer.BadParameter(f"Owner bootstrap unavailable ({exc.status})") from None
    typer.echo("Instance owner created")


@auth_app.command()
def link(provider: str, subject: str, user_id: UUID) -> None:
    """Explicitly link an admitted provider subject to an existing core user as an operator.

    This can give an external identity the existing owner's rights, so it is never
    inferred from email/roles or exposed as an unauthenticated HTTP operation.
    """
    settings = command_settings()

    async def connect() -> None:
        """Own the database lifecycle around the explicit operator mapping."""
        async with Database(settings) as database:
            await IdentityMappings(database, settings, provider=provider).link(subject, user_id)

    try:
        asyncio.run(connect())
    except AuthError as exc:
        raise typer.BadParameter(f"Identity linking unavailable ({exc.status})") from None
    typer.echo("External identity linked")


@auth_app.command()
def owner(user_id: UUID, revoke: bool = typer.Option(False, "--revoke")) -> None:
    """Grant instance ownership, or revoke it while preserving at least one owner.

    Run only from a trusted operator shell after verifying the exact account ID
    through its session response or identity mapping. Changes use the CLI audit actor.
    """
    settings = command_settings()

    async def update() -> None:
        """Dispose the temporary database even if access management is rejected."""
        async with Database(settings) as database:
            await OwnerAccess(database).set_owner(user_id, enabled=not revoke)

    try:
        asyncio.run(update())
    except AuthError as exc:
        raise typer.BadParameter(f"Instance owner update unavailable ({exc.status})") from None
    typer.echo("Instance owner access revoked" if revoke else "Instance owner access granted")
