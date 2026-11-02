"""Operator-only bounded encryption-key rotation without background dispatch."""

import asyncio
import json
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

import typer
from tinypods_cli.runtime import OperatorValidator, command_settings
from tinypods_core.audit import audit_context
from tinypods_core.auth import Principal
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.secrets.crypto import SecretError
from tinypods_core.secrets.providers import encryption_provider
from tinypods_core.secrets.service import RotationResult, SecretService

secrets_app = typer.Typer(help="Manage encrypted cluster credential keys.", add_completion=False)


@secrets_app.command("rotate-key")
def rotate_key(
    batch_size: Annotated[
        int, typer.Option(min=1, max=1000, help="Maximum versions in this resumable atomic batch.")
    ] = 100,
) -> None:
    """Re-encrypt one batch using the configured active key, preserving logical versions.

    Retain old keys and repeat until remaining is zero, then verify backup recovery
    before retiring any key. Concurrent locked rows remain in the reported count.
    """
    settings = command_settings()

    async def rotate() -> RotationResult:
        """Own and close the operator session, including after failure or cancellation."""
        async with Database(settings) as database:
            principal = Principal(
                UUID(int=0),
                "system:secret-key-rotation",
                True,
                "local-operator",
                credential_kind="operator",
            )
            async with database.session() as session:
                service = SecretService(
                    session,
                    AuthorizationService(OperatorValidator()),
                    encryption_provider(settings),
                )
                return await service.rotate(principal, batch_size)

    try:
        with audit_context("system:secret-key-rotation"):
            result = asyncio.run(rotate())
    except SecretError as exc:
        typer.echo(json.dumps({"status": "failed", "code": exc.code}))
        raise typer.Exit(1) from None
    typer.echo(json.dumps({"status": "rotated", **asdict(result)}))
