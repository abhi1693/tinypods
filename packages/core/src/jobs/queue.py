"""Explicit bounded sync RQ boundary, isolated from async application callers."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry as RedisRetry
from rq import Queue, Retry
from rq.job import JobStatus as RQStatus
from rq.serializers import JSONSerializer
from sqlalchemy import update
from sqlmodel import col, select
from tinypods_core.db import Database
from tinypods_core.jobs.models import Job, JobStatus
from tinypods_core.jobs.tasks import TASKS
from tinypods_core.settings import ConfigurationError, Settings

ENTRYPOINT = "tinypods_core.jobs.execution.execute"
RECONCILE = "tinypods_core.jobs.execution.reconcile"
ACTIVE = frozenset((RQStatus.QUEUED, RQStatus.STARTED, RQStatus.SCHEDULED, RQStatus.DEFERRED))


@contextmanager
def connection(settings: Settings) -> Iterator[Redis]:
    """Own a synchronous client only within CLI/worker or bounded thread boundaries."""
    if settings.valkey_url is None:
        raise ConfigurationError("valkey_url is required for job delivery")
    with Redis.from_url(
        settings.valkey_url.get_secret_value(),
        socket_connect_timeout=1,
        socket_timeout=2,
        retry=RedisRetry(NoBackoff(), 0),
    ) as client:
        yield client


def queue_for(settings: Settings, client: Redis) -> Queue:
    """Select one instance-specific JSON queue, never accepting a caller's handler."""
    return Queue(settings.job_queue, connection=client, serializer=JSONSerializer)


def enqueue(settings: Settings, job: Job) -> bool:
    """Submit a committed identity unless RQ already has it; no database mutation.

    This blocking adapter runs in a thread from async callers. Socket waits have
    finite limits and clients close on exit. The queue is trusted infrastructure;
    JSON avoids pickle but is not permission to expose Redis write access.
    """
    task = TASKS[job.task]
    with connection(settings) as client:
        queue = queue_for(settings, client)
        existing = queue.fetch_job(str(job.id))
        if existing is not None:
            return False
        options = dict(
            job_id=str(job.id),
            args=(str(job.id),),
            job_timeout=task.timeout,
            result_ttl=86400,
            failure_ttl=604800,
            retry=Retry(max=len(task.retry_intervals), interval=list(task.retry_intervals)),
            description="TinyPods job " + str(job.id),
        )
        if job.scheduled_for is not None and job.scheduled_for > datetime.now(UTC):
            queue.enqueue_at(job.scheduled_for, ENTRYPOINT, **options)
        else:
            queue.enqueue(ENTRYPOINT, **options)
    return True


async def submit(database: Database, settings: Settings, identity: UUID) -> bool:
    """Best-effort post-commit submission using fresh sessions; retain work on outage.

    Returns:
        True if accepted/already present in RQ; False on absent work or queue outage.
        The committed Job remains recoverable; callers must not undo business work.
    """
    async with database.session() as session:
        job = await session.get(Job, identity)
        if job is None or job.status != JobStatus.PENDING:
            return False
    try:
        await asyncio.to_thread(enqueue, settings, job)
    except RedisError:
        return False
    async with database.transaction() as session:
        await session.exec(
            update(Job)
            .where(col(Job.id) == identity, col(Job.status) == JobStatus.PENDING)
            .values(status=JobStatus.QUEUED)
        )
    return True


def rq_status(settings: Settings, identity: UUID) -> RQStatus | None:
    """Read queue state with a short owned client, never infer completion from absence."""
    with connection(settings) as client:
        job = queue_for(settings, client).fetch_job(str(identity))
        return job.get_status() if job is not None else None


async def recover(database: Database, settings: Settings, after: UUID | None = None) -> int:
    """Reconcile one bounded batch with RQ; never blindly retry uncertain running work.

    Queued work missing from Valkey becomes pending and is resubmitted. Running
    work with a failed/missing/finished RQ record is marked failed for operator
    review, not re-executed. This is not external-side-effect reconciliation.
    """
    # Terminal application state is authoritative even after RQ history expires.
    # Inspect only unfinished work, in UUID order so continuation pages advance
    # past active jobs instead of repeatedly selecting the same oldest batch.
    async with database.session() as session:
        query = select(Job).where(
            col(Job.status).in_((JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING))
        )
        if after is not None:
            query = query.where(col(Job.id) > after)
        rows = list(
            (await session.exec(query.order_by(col(Job.id)).limit(settings.job_batch_size))).all()
        )
    submitted = 0
    for job in rows:
        status = await asyncio.to_thread(rq_status, settings, job.id)
        # Queue loss is not the same as a delayed/retrying/running RQ job. Leave
        # active deliveries alone: RQ already owns their execution and schedule.
        if status in ACTIVE:
            continue
        # A running handler may have produced effects before dying or losing its
        # completion record. Missing/terminal RQ state cannot prove a safe retry.
        # Likewise, don't resurrect a job RQ has exhausted/stopped/completed.
        if job.status == JobStatus.RUNNING or status in (
            RQStatus.FAILED,
            RQStatus.STOPPED,
            RQStatus.CANCELED,
            RQStatus.FINISHED,
        ):
            async with database.transaction() as session:
                await session.exec(
                    update(Job)
                    .where(col(Job.id) == job.id, col(Job.status) == job.status)
                    .values(status=JobStatus.FAILED, completed=datetime.now(UTC))
                )
            continue
        # Only work that has not started reaches submission again. Restore pending
        # eligibility for queued work, then let the normal submission path check RQ.
        # Conditional updates and submit's fresh read preserve racing state changes.
        if job.status == JobStatus.QUEUED:
            async with database.transaction() as session:
                await session.exec(
                    update(Job)
                    .where(col(Job.id) == job.id, col(Job.status) == JobStatus.QUEUED)
                    .values(status=JobStatus.PENDING)
                )
        submitted += int(await submit(database, settings, job.id))
    # Yield large scans back to RQ in bounded pages; recurrence is managed by the
    # top-level reconciliation entry point, not restarted for each continuation.
    if len(rows) == settings.job_batch_size:
        await asyncio.to_thread(continue_recovery, settings, rows[-1].id)
    return submitted


def schedule_reconciliation(settings: Settings) -> None:
    """Keep one scheduled reconciliation on the initial single-worker deployment.

    RQ owns timing. A hard kill can bypass recurrence; worker startup repairs it.
    This function does not implement multi-worker election or an independent loop.
    """
    with connection(settings) as client:
        queue = queue_for(settings, client)
        # Reuse an existing occurrence on restart; otherwise seed the chain lost
        # with queue data or a hard kill before the previous run could reschedule.
        for identity in queue.scheduled_job_registry.get_job_ids():
            job = queue.fetch_job(identity)
            if job is not None and job.func_name == RECONCILE:
                return
        queue.enqueue_in(
            timedelta(minutes=settings.job_reconcile_minutes),
            RECONCILE,
            job_timeout=60,
            result_ttl=0,
            failure_ttl=86400,
            description="TinyPods reconciliation",
        )


def continue_recovery(settings: Settings, after: UUID) -> None:
    """Yield between bounded recovery pages without starving work behind active jobs."""
    with connection(settings) as client:
        queue_for(settings, client).enqueue(
            RECONCILE,
            str(after),
            job_timeout=60,
            result_ttl=0,
            failure_ttl=86400,
            description="TinyPods reconciliation continuation",
        )
