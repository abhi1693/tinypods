"""Transactional job creation and existing owner-only service authorization."""

import json
from datetime import datetime
from uuid import UUID

from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.auth import AuthorizationService, Principal, Scope
from tinypods_core.jobs.models import Job
from tinypods_core.jobs.tasks import TARGETS, TASKS
from tinypods_core.json_types import JsonRecord


async def create_job(
    session: AsyncSession,
    task: str,
    data: JsonRecord,
    *,
    requested_by_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    scheduled_for: datetime | None = None,
) -> Job:
    """Append validated work in the caller's transaction; never commit or enqueue.

    Args:
        session: Transaction owning the related business changes.
        task: Code-owned registered task name, never an import path.
        data: Safe task arguments validated by the registered schema.
        requested_by_id: Initiating actor, not an ownership grant; None for system work.
        target_type: Optional code-owned resolver key.
        target_id: Optional related UUID, paired with target_type.
        scheduled_for: Optional timezone-aware earliest execution time.

    Returns:
        Pending work; callers must commit successfully before submitting its ID to RQ.

    Raises:
        ValueError: Task, arguments, schedule or related object are invalid.
    """
    definition = TASKS.get(task)
    if definition is None:
        raise ValueError("Unknown job task")
    validated = definition.schema.model_validate(data)
    encoded = json.dumps(validated.model_dump(mode="json"), allow_nan=False)
    if len(encoded.encode()) > 16384:
        raise ValueError("Job arguments exceed 16384 bytes")
    if scheduled_for is not None and scheduled_for.utcoffset() is None:
        raise ValueError("Job schedule must be timezone-aware")
    if (target_type is None) != (target_id is None):
        raise ValueError("Job target fields must be paired")
    if target_type is not None and target_id is not None:
        resolver = TARGETS.get(target_type)
        if resolver is None or not await resolver(session, target_id):
            raise ValueError("Unknown or missing job target")
    job = Job(
        task=task,
        data=json.loads(encoded),
        requested_by_id=requested_by_id,
        target_type=target_type,
        target_id=target_id,
        scheduled_for=scheduled_for,
    )
    session.add(job)
    return job


async def require_job_access(authorization: AuthorizationService, principal: Principal) -> None:
    """Use existing platform-owner policy; requester/target links never grant access."""
    await authorization.require_access(principal, Scope.CATALOG_ADMIN)
