"""Owner-authorized job status without exposing task arguments or raw failures."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from tinypods_api.auth import AuthorizationDependency, PrincipalDependency
from tinypods_api.http.dependencies import DatabaseDependency
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.jobs import Job
from tinypods_core.jobs.service import require_job_access


class JobRead(BaseModel):
    """Expose safe lifecycle metadata; task arguments and raw failures stay private."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    task: str
    status: str
    requested_by_id: UUID | None
    created: datetime
    scheduled_for: datetime | None
    started: datetime | None
    completed: datetime | None


router = APIRouter(route_class=RateLimitedRoute, tags=["jobs"])


@router.get(
    "/jobs/{id}",
    response_model=JobRead,
    operation_id=build_operation_id("platform", "job", "retrieve"),
    openapi_extra=EndpointRatePolicy(name="jobRead", limit=60).openapi_extra(),
)
async def read_job(
    id: UUID,
    database: DatabaseDependency,
    authorization: AuthorizationDependency,
    principal: PrincipalDependency,
) -> JobRead:
    """Read one Job using current platform-owner policy, regardless of its requester."""
    await require_job_access(authorization, principal)
    async with database.session() as session:
        job = await session.get(Job, id)
        if job is None:
            raise HTTPException(404)
        return JobRead.model_validate(job)
