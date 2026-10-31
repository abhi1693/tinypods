"""HTTP error boundaries expose deliberate messages, never exception internals."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from tinypods_api.contracts import ErrorResponse, ValidationIssue

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
    payload = ErrorResponse(
        code=code,
        message=message,
        request_id=UUID(request.state.request_id),
        issues=issues or [],
    )
    return JSONResponse(payload.model_dump(mode="json"), status_code=status, headers=headers)


async def http_error(request: Request, exc: Exception) -> JSONResponse:
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
    assert isinstance(exc, RequestValidationError)
    issues = [
        ValidationIssue(location=list(error["loc"]), code=error["type"]) for error in exc.errors()
    ]
    return error_response(
        request, 422, "validation_error", "Request validation failed", issues=issues
    )


async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(request, 500, "internal_error", "An unexpected error occurred")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
