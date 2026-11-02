"""Operator-only repository sync using the same persistence service as HTTP administration."""

import asyncio
import json
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

import typer
from sqlmodel import col, select
from tinypods_cli.runtime import OperatorValidator, command_settings
from tinypods_core.applications.service import DefinitionError
from tinypods_core.audit import audit_context
from tinypods_core.auth import Principal
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories import ChartRepository
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.service import RepositorySync
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.transport import MetadataHTTP

repositories_app = typer.Typer(help="Manage external chart repositories.", add_completion=False)


@repositories_app.command("sync")
def sync(
    repository_id: Annotated[
        UUID | None, typer.Argument(help="One source ID; omit to sync all enabled sources.")
    ] = None,
    initial_versions: Annotated[
        int, typer.Option(min=1, max=10000, help="Latest versions per newly synced chart.")
    ] = 3,
) -> None:
    """Persist discovered chart/version metadata, reporting each source's outcome.

    Each source commits atomically. A later failed source does not undo successful
    earlier sources; results report this explicitly and failures exit nonzero.
    """
    settings = command_settings()

    async def synchronize() -> tuple[list[dict[str, object]], bool]:
        """Own and close all database resources, including on errors or cancellation."""
        async with Database(settings) as database:
            outcomes: list[dict[str, object]] = []
            failed = False
            principal = Principal(
                UUID(int=0),
                "system:repository-sync",
                True,
                "local-operator",
                credential_kind="operator",
            )
            service = RepositorySync(
                database,
                AuthorizationService(OperatorValidator()),
                RepositoryClient(MetadataHTTP(settings)),
            )
            if repository_id is None:
                async with database.session() as session:
                    identities = list(
                        (
                            await session.exec(
                                select(ChartRepository.id)
                                .where(col(ChartRepository.enabled).is_(True))
                                .order_by(col(ChartRepository.id))
                            )
                        ).all()
                    )
            else:
                identities = [repository_id]
            for identity in identities:
                try:
                    result = await service.run(principal, identity, initial_versions)
                    outcomes.append({"status": "synced", **asdict(result)})
                except (SourceError, DefinitionError) as exc:
                    failed = True
                    outcomes.append(
                        {
                            "status": "failed",
                            "repository_id": identity,
                            "code": exc.code
                            if isinstance(exc, SourceError)
                            else "http_" + str(exc.status),
                        }
                    )
            return outcomes, failed

    with audit_context("system:repository-sync"):
        outcomes, failed = asyncio.run(synchronize())
    typer.echo(json.dumps({"repositories": outcomes}, default=str))
    if failed:
        raise typer.Exit(1)
