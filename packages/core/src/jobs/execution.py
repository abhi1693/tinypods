"""Fixed JSON RQ entry points; fresh async resources per job, safe failure reporting."""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from rq import get_current_job
from sqlalchemy import update
from sqlmodel import col
from tinypods_core.audit import audit_context
from tinypods_core.db import Database
from tinypods_core.jobs.models import Job, JobStatus
from tinypods_core.jobs.queue import recover, schedule_reconciliation
from tinypods_core.jobs.tasks import TARGETS, TASKS
from tinypods_core.settings import Settings, load_settings

logger = logging.getLogger("tinypods.jobs")


class JobExecutionError(RuntimeError):
    """Expose only a stable failure classification to RQ logs and registries."""


async def run(database: Database, identity: UUID) -> None:
    """Claim pending work atomically, validate its contract/target, and run its handler.

    Duplicate deliveries of running/terminal jobs do nothing. No lease or takeover
    is implied. Tasks must keep external effects duplicate-safe before registration.
    """
    # Commit the conditional claim before calling application code. Competing
    # deliveries cannot both change pending/queued to running, and no database
    # transaction needs to stay open while the handler performs external I/O.
    async with database.transaction() as session:
        claimed = await session.exec(
            update(Job)
            .where(
                col(Job.id) == identity, col(Job.status).in_((JobStatus.PENDING, JobStatus.QUEUED))
            )
            .values(status=JobStatus.RUNNING, started=datetime.now(UTC))
            .returning(col(Job.id))
        )
        # A duplicate or stale delivery must not restart running/terminal work.
        if claimed.scalar_one_or_none() is None:
            return
    try:
        # Resolve only code-owned contracts and revalidate persisted inputs/target:
        # they may have changed or disappeared since submission. The detached Job
        # lets the handler apply its domain's authorization with its own sessions.
        async with database.session() as session:
            job = await session.get(Job, identity)
            assert job is not None
            definition = TASKS[job.task]
            inputs = definition.schema.model_validate(job.data)
            if job.target_type is not None and job.target_id is not None:
                resolver = TARGETS[job.target_type]
                if not await resolver(session, job.target_id):
                    raise JobExecutionError("Job target is missing")
        # The read session is closed; handlers never inherit our transaction.
        await definition.run(database, inputs, job)
    except Exception:
        # RQ owns retry timing and budget. Pending makes the Job eligible for its
        # next delivery; exhausting that budget makes the failure durable instead.
        # Cancellation/hard death bypass this ordinary-error path: running work
        # then needs reconciliation, not an assumption that its effects rolled back.
        current = get_current_job()
        retrying = current is not None and bool(current.retries_left)
        async with database.transaction() as session:
            await session.exec(
                update(Job)
                .where(col(Job.id) == identity, col(Job.status) == JobStatus.RUNNING)
                .values(
                    status=JobStatus.PENDING if retrying else JobStatus.FAILED,
                    completed=None if retrying else datetime.now(UTC),
                )
            )
        # Keep arbitrary handler exceptions/arguments out of RQ's logs/registry.
        raise JobExecutionError(
            "Job task failed; inspect safe status and worker configuration"
        ) from None
    # Record success separately from the handler. If this commit fails, effects
    # may already exist: leaving running state prevents an automatic repeat, and
    # recovery reports the uncertain outcome for operator/domain reconciliation.
    # The status guard also avoids overwriting a state changed by recovery.
    async with database.transaction() as session:
        await session.exec(
            update(Job)
            .where(col(Job.id) == identity, col(Job.status) == JobStatus.RUNNING)
            .values(status=JobStatus.SUCCEEDED, completed=datetime.now(UTC))
        )


def execute(identity: str) -> None:
    """Run the sole domain-job RQ entry point with an exact canonical UUID argument."""
    parsed = UUID(identity)
    if str(parsed) != identity:
        raise ValueError("Invalid job identity")
    settings = load_settings()

    async def owned() -> None:
        async with Database(settings) as database:
            await run(database, parsed)

    with audit_context("system:worker"):
        asyncio.run(owned())


def reconcile(after: str | None = None) -> None:
    """Perform a bounded recovery pass and schedule the next even on ordinary failure."""
    settings = load_settings()
    try:
        asyncio.run(bootstrap(settings, UUID(after) if after is not None else None))
    except Exception:
        logger.error("Job reconciliation failed")
        raise JobExecutionError("Job reconciliation failed") from None
    finally:
        if after is None:
            schedule_reconciliation(settings)


async def bootstrap(settings: Settings, after: UUID | None = None) -> int:
    """Own database resources for startup and RQ-scheduled recovery."""
    async with Database(settings) as database:
        return await recover(database, settings, after)
