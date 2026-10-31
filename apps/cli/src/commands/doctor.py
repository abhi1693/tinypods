"""Configuration and optional async database diagnostics."""

import asyncio
import json
from typing import Annotated

import typer
from tinypods_cli.runtime import command_settings
from tinypods_core.db import Database
from tinypods_core.settings import Settings


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
