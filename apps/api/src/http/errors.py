"""HTTP error boundaries expose deliberate messages, never exception internals."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from tinypods_api.http.contracts import ErrorResponse, ValidationIssue

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": HTTPStatus(code).phrase}
    for code in (400, 401, 403, 404, 405, 409, 422, 429, 500, 503)
}


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    issues: list[ValidationIssue] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Serialize the public error DTO using middleware-owned request correlation.

    Args:
        request: Current request carrying middleware-owned correlation state.
        status: HTTP status code returned to the client.
        code: Stable machine-readable public error identifier.
        message: Deliberate public response message, not an exception dump.
        issues: Optional validation locations and codes to include in the response.
        headers: Optional protocol response headers to preserve.

    Returns:
        Public error JSON with correlation metadata and the selected HTTP status.
    """
    payload = ErrorResponse(
        code=code,
        message=message,
        request_id=UUID(request.state.request_id),
        issues=issues or [],
    )
    return JSONResponse(payload.model_dump(mode="json"), status_code=status, headers=headers)


async def http_error(request: Request, exc: Exception) -> JSONResponse:
    """Translate HTTP errors using standard phrases rather than exception details.

    Args:
        request: Current request carrying middleware-owned correlation state.
        exc: Exception being translated or summarized at this boundary.

    Returns:
        Public error JSON with correlation metadata and the selected HTTP status.
    """
    assert isinstance(exc, HTTPException)
    # HTTPException.detail may contain SQL, credentials or arbitrary dependency exceptions.
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "Request failed"
    return error_response(
        request, exc.status_code, f"http_{exc.status_code}", phrase, headers=exc.headers
    )


async def validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Translate input errors into public locations and codes without rejected values.

    Args:
        request: Current request carrying middleware-owned correlation state.
        exc: Exception being translated or summarized at this boundary.

    Returns:
        Public error JSON with correlation metadata and the selected HTTP status.
    """
    assert isinstance(exc, RequestValidationError)
    issues = [
        ValidationIssue(location=list(error["loc"]), code=error["type"]) for error in exc.errors()
    ]
    return error_response(
        request, 422, "validation_error", "Request validation failed", issues=issues
    )


async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Return a fixed public server-error response without exception internals.

    Args:
        request: Current request carrying middleware-owned correlation state.
        exc: Exception being translated or summarized at this boundary.

    Returns:
        Public error JSON with correlation metadata and the selected HTTP status.
    """
    return error_response(request, 500, "internal_error", "An unexpected error occurred")


def install_error_handlers(app: FastAPI) -> None:
    """Register deliberate HTTP, validation and unexpected-error translations.

    Args:
        app: Application or downstream ASGI callable being composed.
    """
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
