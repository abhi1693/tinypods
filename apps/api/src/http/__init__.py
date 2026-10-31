"""Public HTTP DTOs, request dependencies and common error composition."""

from tinypods_api.http.contracts import (
    ErrorResponse,
    LivenessResponse,
    OperationStatus,
    Page,
    PageParams,
    PlatformInfoResponse,
    ReadinessResponse,
    RequestDTO,
    ValidationIssue,
)
from tinypods_api.http.dependencies import get_session
from tinypods_api.http.errors import ERROR_RESPONSES, install_error_handlers
from tinypods_api.http.middleware import RequestLoggingMiddleware
from tinypods_api.http.operations import build_operation_id
from tinypods_api.http.schema import export_schema, render_schema

__all__ = [
    "build_operation_id",
    "ERROR_RESPONSES",
    "ErrorResponse",
    "LivenessResponse",
    "OperationStatus",
    "Page",
    "PageParams",
    "PlatformInfoResponse",
    "ReadinessResponse",
    "RequestDTO",
    "RequestLoggingMiddleware",
    "ValidationIssue",
    "export_schema",
    "get_session",
    "install_error_handlers",
    "render_schema",
]
