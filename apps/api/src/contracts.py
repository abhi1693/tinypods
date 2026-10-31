"""Public HTTP DTOs. Database and plugin models do not belong here."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageParams(RequestDTO):
    """Offset pagination; callers must use a deterministic ordering with a unique tie-breaker."""

    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Page[T](BaseModel):
    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class OperationStatus(BaseModel):
    operation_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


class ValidationIssue(BaseModel):
    location: list[str | int]
    code: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: UUID
    issues: list[ValidationIssue] = Field(default_factory=list)


class PlatformInfoResponse(BaseModel):
    """Expose public platform identity without process configuration or credentials.

    Attributes:
        service: Static service display name.
        version: Version of the installed TinyPods core distribution."""

    service: Literal["TinyPods"] = "TinyPods"
    version: str
