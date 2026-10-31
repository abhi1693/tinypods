"""Deterministic, environment-independent core contract export."""

import inspect
import json
from pathlib import Path
from typing import Annotated, cast

import typer
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel
from tinypods_core.json_types import JsonRecord
from tinypods_core.settings import Settings

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


def export_schema(application: FastAPI) -> JsonRecord:
    """Reject undocumented routes, ambiguous IDs and missing response DTOs."""
    ids: set[str] = set()
    for context in iter_route_contexts(application.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not context.include_in_schema:
            continue
        if not route.operation_id or route.operation_id in ids:
            raise ValueError("Each API operation needs a unique explicit operation_id")
        if len(route.methods or set()) != 1:
            raise ValueError("Each API operation must declare exactly one method")
        if not inspect.getdoc(route.endpoint) or not route.description.strip():
            raise ValueError("Each API operation needs a docstring and public description")
        ids.add(route.operation_id)
        if route.status_code != 204 and (
            not isinstance(route.response_model, type)
            or not issubclass(route.response_model, BaseModel)
        ):
            raise ValueError("Each API operation needs an explicit response model")
    application.openapi_schema = None
    return cast(JsonRecord, application.openapi())


def render_schema(application: FastAPI) -> str:
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
    from tinypods_api.main import create_app

    # Explicit defaults bypass deployment settings and never connect to infrastructure.
    rendered = render_schema(create_app(Settings.model_construct()))
    if check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            raise typer.Exit(1)
    else:
        output.write_text(rendered, encoding="utf-8")


def cli() -> None:
    app()
