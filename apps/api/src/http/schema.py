"""Deterministic, environment-independent core contract export."""

import inspect
import json
import re
from pathlib import Path
from typing import Annotated, cast

import typer
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel
from tinypods_core.json_types import JsonRecord
from tinypods_core.logging import shutdown_logging
from tinypods_core.settings import Settings

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


def export_schema(application: FastAPI) -> JsonRecord:
    """Reject undocumented routes, ambiguous IDs and missing response DTOs.

    Args:
        application: Application whose public HTTP contract is inspected.

    Returns:
        Validated OpenAPI schema for every included public route, including nested routers.

    Raises:
        ValueError: A public operation has missing documentation, an ambiguous ID or no DTO.
    """
    ids: set[str] = set()
    for context in iter_route_contexts(application.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not context.include_in_schema:
            continue
        if not route.operation_id or route.operation_id in ids:
            raise ValueError("Each API operation needs a unique explicit operation_id")
        if not re.fullmatch(r"[a-z][A-Za-z0-9]*", route.operation_id):
            raise ValueError("Operation IDs must use lowerCamelCase")
        if len(route.methods or set()) != 1:
            raise ValueError("Each API operation must declare exactly one method")
        if not inspect.getdoc(route.endpoint) or not route.description.strip():
            raise ValueError("Each API operation needs a docstring and public description")
        ids.add(route.operation_id)
        redirect = route.response_class is RedirectResponse and route.status_code == 303
        if (
            route.status_code != 204
            and not redirect
            and (
                not isinstance(route.response_model, type)
                or not issubclass(route.response_model, BaseModel)
            )
        ):
            raise ValueError("Each API operation needs an explicit response model")
    application.openapi_schema = None
    return cast(JsonRecord, application.openapi())


def render_schema(application: FastAPI) -> str:
    """Render validated public OpenAPI data as deterministic sorted JSON.

    Args:
        application: Application whose public HTTP contract is inspected.

    Returns:
        Sorted, indented OpenAPI JSON ending in a single newline.

    Raises:
        ValueError: The application contains an invalid public route contract.
    """
    return (
        json.dumps(export_schema(application), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


@app.command()
def main(
    output: Annotated[Path, typer.Option("--output")] = Path("openapi.json"),
    check: Annotated[
        bool, typer.Option("--check", help="Fail if the contract has changed.")
    ] = False,
) -> None:
    """Export or compare the schema without starting runtime infrastructure.

    Args:
        output: Schema destination, or committed artifact checked for drift.
        check: Whether to compare the generated artifact without writing it.
    """
    from tinypods_api.main import create_app

    try:
        # Explicit defaults bypass deployment settings and never connect to infrastructure.
        rendered = render_schema(
            create_app(Settings.model_construct(), include_disabled_providers=True)
        )
        if check:
            if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
                raise typer.Exit(1)
        else:
            output.write_text(rendered, encoding="utf-8")
    finally:
        shutdown_logging()


def cli() -> None:
    """Dispatch the offline schema export command through Typer."""
    app()
