"""Explicit audit maintenance with dry-run defaults and structured results."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated

import typer
from tinypods_cli.runtime import command_settings
from tinypods_core.audit.maintenance import cleanup_object_types, prune_records, prune_window
from tinypods_core.db import migration_engine

audit_app = typer.Typer(help="Inspect and explicitly maintain audit history.", add_completion=False)


@audit_app.command("cleanup-types")
def cleanup_types(
    apply: Annotated[
        bool, typer.Option("--apply", help="Delete only unreferenced stale types.")
    ] = False,
) -> None:
    """List stale object types, retaining any referenced by audit history.

    Args:
        apply: Explicit deletion; the default is a read-only report.
    """
    engine = migration_engine(command_settings())
    try:
        with engine.begin() as connection:
            rows = cleanup_object_types(connection, apply=apply)
        typer.echo(
            json.dumps(
                {
                    "apply": apply,
                    "stale": [asdict(row) for row in rows],
                    "deleted": sum(row.deleted for row in rows),
                    "retained": sum(
                        bool(row.changed_references or row.related_references) for row in rows
                    ),
                },
                default=str,
            )
        )
    finally:
        engine.dispose()


@audit_app.command("prune")
def prune(
    older_than_days: Annotated[
        int | None, typer.Option(help="Delete records strictly older than this age.")
    ] = None,
    start: Annotated[
        str | None, typer.Option(help="Inclusive ISO 8601 timestamp with timezone.")
    ] = None,
    end: Annotated[
        str | None, typer.Option(help="Exclusive ISO 8601 timestamp with timezone.")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Explicitly delete matched history.")
    ] = False,
    batch_size: Annotated[
        int, typer.Option(min=1, help="Rows per delete statement, not a total limit.")
    ] = 500,
) -> None:
    """Count or explicitly prune old history by age OR a half-open timestamp range.

    Args:
        older_than_days: Positive age in days, mutually exclusive with range options.
        start: Inclusive timestamp with an explicit timezone offset or Z.
        end: Exclusive timestamp with an explicit timezone offset or Z.
        apply: Execute deletion; omitted by default for a dry run.
        batch_size: Positive bounded processing size.
    """
    try:
        window = prune_window(older_than_days, start, end, now=datetime.now(UTC))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    engine = migration_engine(command_settings())
    try:
        with engine.begin() as connection:
            matched, deleted = prune_records(connection, window, apply=apply, batch_size=batch_size)
        typer.echo(
            json.dumps(
                {
                    "apply": apply,
                    "start": window.start.isoformat() if window.start else None,
                    "end": window.end.isoformat(),
                    "interval": "[start,end)",
                    "matched": matched,
                    "deleted": deleted,
                }
            )
        )
    finally:
        engine.dispose()
