"""Public HTTP DTOs. Database and plugin models do not belong here."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RequestDTO(BaseModel):
    """Reject undeclared fields in public request payloads."""

    model_config = ConfigDict(extra="forbid")


class PageParams(RequestDTO):
    """Offset pagination; callers must use a deterministic ordering with a unique tie-breaker."""

    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Page[T](BaseModel):
    """Carry typed page items, total count and the pagination values used to select them."""

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class OperationStatus(BaseModel):
    """Expose an operation identifier and its queued, running or terminal state."""

    operation_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]


class LivenessResponse(BaseModel):
    """Report process liveness independently of dependency availability."""

    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    """Report aggregate readiness and the individual boolean dependency outcomes."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


class ValidationIssue(BaseModel):
    """Expose a rejected input location and code without echoing its value."""

    location: list[str | int]
    code: str


class ErrorResponse(BaseModel):
    """Carry a public error code, message, correlation ID and optional validation issues."""

    code: str
    message: str
    request_id: UUID
    issues: list[ValidationIssue] = Field(default_factory=list)


class PlatformInfoResponse(BaseModel):
    """Expose public platform identity without process configuration or credentials.

    Attributes:
        service: Static service display name.
        version: Version of the installed TinyPods core distribution.
    """

    service: Literal["TinyPods"] = "TinyPods"
    version: str
