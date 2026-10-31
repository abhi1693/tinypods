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
    """Whether the service is running."""

    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    """Whether the service and its required connections are ready."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


class ValidationIssue(BaseModel):
    """A field that needs to be corrected."""

    location: list[str | int]
    code: str


class ErrorResponse(BaseModel):
    """An explanation of why the request could not be completed."""

    code: str
    message: str
    request_id: UUID
    issues: list[ValidationIssue] = Field(default_factory=list)


class PlatformInfoResponse(BaseModel):
    """Service name and installed version."""

    service: Literal["TinyPods"] = "TinyPods"
    version: str
