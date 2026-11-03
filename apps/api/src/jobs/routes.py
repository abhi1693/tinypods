"""Fixed diagnostic submission and owner-authorized job status without argument dumps."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from tinypods_api.auth import AuthorizationDependency, PrincipalDependency
from tinypods_api.http.dependencies import DatabaseDependency
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.jobs import Job
from tinypods_core.jobs.queue import submit
from tinypods_core.jobs.service import create_job, require_job_access


class CheckRequest(BaseModel):
    """Schedule the single supported diagnostic, without caller-selected handlers."""

    model_config = ConfigDict(extra="forbid")
    scheduled_for: AwareDatetime | None = None


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


@router.post(
    "/jobs/database-check",
    status_code=202,
    response_model=JobRead,
    operation_id=build_operation_id("platform", "job", "checkDatabase"),
    openapi_extra=EndpointRatePolicy(name="jobDatabaseCheck", limit=10).openapi_extra(),
)
async def check_database(
    payload: CheckRequest,
    request: Request,
    database: DatabaseDependency,
    authorization: AuthorizationDependency,
    principal: PrincipalDependency,
) -> JobRead:
    """Accept an owner-only database diagnostic and submit after successful commit.

    HTTP 202 acknowledges durable acceptance, not queue delivery or completion.
    Queue outages leave pending work for startup/periodic reconciliation.
    """
    await require_job_access(authorization, principal)
    async with database.transaction() as session:
        job = await create_job(
            session,
            "system.database_check",
            {},
            requested_by_id=principal.user_id,
            scheduled_for=payload.scheduled_for,
        )
    await submit(database, request.app.state.settings, job.id)
    async with database.session() as session:
        stored = await session.get(Job, job.id)
        return JobRead.model_validate(stored)


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
