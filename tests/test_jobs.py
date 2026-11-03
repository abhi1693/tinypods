"""Real PostgreSQL/Valkey regressions for minimal durable jobs and RQ delivery."""

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from job_fixtures import TEST_TASKS
from pydantic import SecretStr
from redis.exceptions import ConnectionError, OutOfMemoryError
from rq.job import JobStatus as RQStatus
from sqlalchemy import text
from test_auth import auth_settings as auth_settings
from test_auth import credentials
from tinypods_api.main import create_app
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database, MigrationManager
from tinypods_core.db.models import User
from tinypods_core.jobs import Job, JobStatus, execution, queue, tasks
from tinypods_core.jobs.service import create_job
from tinypods_core.settings import ConfigurationError, Settings


@pytest.fixture
def job_settings(auth_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Use a unique RQ queue within disposable service containers."""
    from tinypods_core.jobs import service

    for module in (tasks, service, queue, execution):
        monkeypatch.setattr(module, "TASKS", TEST_TASKS)
    return auth_settings.model_copy(update={"job_queue": "jobs-" + uuid4().hex})


async def seed(database: Database, **options: object) -> Job:
    """Commit one registered test task in an explicit business transaction."""
    async with database.transaction() as session:
        return await create_job(session, "tests.record", {}, **options)


def environment(settings: Settings) -> dict[str, str]:
    """Pass only disposable service configuration to installed worker processes."""
    return {
        **os.environ,
        "TINYPODS_DATABASE_URL": settings.database_url.get_secret_value(),
        "TINYPODS_VALKEY_URL": settings.valkey_url.get_secret_value(),
        "TINYPODS_JOB_QUEUE": settings.job_queue,
    }


def test_atomic_creation_validation_and_migration(job_settings: Settings) -> None:
    """Commit business/job together, reject invalid input and preserve schema integrity."""

    async def scenario() -> None:
        async with Database(job_settings) as database:
            with pytest.raises(RuntimeError):
                async with database.transaction() as session:
                    session.add(User(username="rollback"))
                    await create_job(session, "tests.record", {})
                    await session.flush()
                    raise RuntimeError("abort")
            async with database.session() as session:
                assert (await session.exec(text("SELECT count(*) FROM jobs"))).scalar() == 0
                for name, data in [
                    ("os.system", {}),
                    ("tests.record", {"extra": "private"}),
                ]:
                    with pytest.raises(ValueError):
                        await create_job(session, name, data)
                with pytest.raises(ValueError, match="timezone"):
                    await create_job(session, "tests.record", {}, scheduled_for=datetime.now())
                with pytest.raises(ValueError, match="paired"):
                    await create_job(session, "tests.record", {}, target_type="auth.user")
                for target in ("auth.user", "arbitrary.table"):
                    with pytest.raises(ValueError, match="target"):
                        await create_job(
                            session,
                            "tests.record",
                            {},
                            target_type=target,
                            target_id=uuid4(),
                        )
            async with database.transaction() as session:
                user = User(username="target")
                session.add(user)
                await session.flush()
                job = await create_job(
                    session, "tests.record", {}, target_type="auth.user", target_id=user.id
                )
                assert "data=" not in repr(job)
            async with database.transaction() as session:
                user = await session.get(User, user.id)
                await session.delete(user)
            with pytest.raises(execution.JobExecutionError):
                await execution.run(database, job.id)
            async with database.session() as session:
                assert (await session.get(Job, job.id)).status == JobStatus.FAILED

    asyncio.run(scenario())
    manager = MigrationManager(job_settings, Path(__file__).resolve().parents[1] / "alembic.ini")
    manager.check()
    with pytest.raises(RuntimeError, match="history"):
        command.downgrade(manager.config(), "0009_application_installations")
    with psycopg.connect(job_settings.database_url.get_secret_value(), autocommit=True) as client:
        client.execute("DELETE FROM jobs")
        for sql in [
            "INSERT INTO jobs (id,task,data,status,created) VALUES (%s,'bad','{}','pending',now())",
            "INSERT INTO jobs (id,task,data,status,created,target_type) "
            "VALUES (%s,'test.task','{}','pending',now(),'auth.user')",
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                client.execute(sql, (uuid4(),))
    command.downgrade(manager.config(), "0009_application_installations")
    manager.upgrade()
    manager.check()


def test_real_worker_submission_duplicates_and_schedule(job_settings: Settings) -> None:
    """Installed RQ worker consumes JSON IDs and completes a registered test task once."""

    async def prepare() -> tuple[UUID, UUID]:
        async with Database(job_settings) as database:
            first = await seed(database)
            later = await seed(database, scheduled_for=datetime.now(UTC) + timedelta(hours=1))
            assert await queue.submit(database, job_settings, first.id)
            assert not await queue.submit(database, job_settings, first.id)
            assert not await queue.submit(database, job_settings, uuid4())
            assert await queue.submit(database, job_settings, later.id)
            assert queue.rq_status(job_settings, later.id) == RQStatus.SCHEDULED
            return first.id, later.id

    first, later = asyncio.run(prepare())
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("job_fixtures.py")), "--burst"],
        env=environment(job_settings),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    async def verify() -> None:
        async with Database(job_settings) as database:
            async with database.session() as session:
                job = await session.get(Job, first)
                assert job.status == JobStatus.SUCCEEDED
                started, completed = job.started, job.completed
            await execution.run(database, first)
            async with database.session() as session:
                job = await session.get(Job, first)
                assert (job.started, job.completed) == (started, completed)
            assert await queue.recover(database, job_settings) == 0

    asyncio.run(verify())
    with queue.connection(job_settings) as client:
        rq = queue.queue_for(job_settings, client)
        scheduled = [rq.fetch_job(identity) for identity in rq.scheduled_job_registry.get_job_ids()]
        assert sum(job.func_name == queue.RECONCILE for job in scheduled) == 1
        assert rq.fetch_job(str(first)).func_name == queue.ENTRYPOINT
        assert rq.fetch_job(str(first)).args == [str(first)]


def test_handoff_outage_and_queue_loss_recovery(job_settings: Settings) -> None:
    """Queue failure and accepted-before-DB-ack interruption retain recoverable identity."""

    async def scenario() -> None:
        async with Database(job_settings) as database:
            job = await seed(database)
            for error in (ConnectionError("private"), OutOfMemoryError("private")):
                with patch.object(queue, "enqueue", side_effect=error):
                    assert not await queue.submit(database, job_settings, job.id)
            # Accepted queue job, simulated interruption before PostgreSQL acknowledgment.
            assert queue.enqueue(job_settings, job)
            assert not queue.enqueue(job_settings, job)
            assert await queue.submit(database, job_settings, job.id)
            with queue.connection(job_settings) as client:
                rq = queue.queue_for(job_settings, client)
                rq.fetch_job(str(job.id)).delete()
            assert await queue.recover(database, job_settings) == 1
            assert queue.rq_status(job_settings, job.id) == RQStatus.QUEUED
            # Running work with a lost queue record is uncertain, never blindly replayed.
            async with database.transaction() as session:
                current = await session.get(Job, job.id)
                current.status = JobStatus.RUNNING
                session.add(current)
            with queue.connection(job_settings) as client:
                queue.queue_for(job_settings, client).fetch_job(str(job.id)).delete()
            assert await queue.recover(database, job_settings) == 0
            async with database.session() as session:
                assert (await session.get(Job, job.id)).status == JobStatus.FAILED

    asyncio.run(scenario())


def test_execution_failure_retry_and_safe_reporting(job_settings: Settings) -> None:
    """Use RQ retry policy without duplicate attempt tables or raw exception disclosure."""

    async def failing(database: Database, inputs: tasks.TaskInput, job: Job) -> None:
        raise RuntimeError("private task diagnostic")

    async def scenario() -> None:
        async with Database(job_settings) as database:
            job = await seed(database)
            definition = replace(tasks.TASKS["tests.record"], run=failing)
            with patch.object(execution, "TASKS", {"tests.record": definition}):
                for retries in (1, 0):
                    with patch.object(
                        execution,
                        "get_current_job",
                        return_value=SimpleNamespace(retries_left=retries),
                    ):
                        with pytest.raises(
                            execution.JobExecutionError, match="Job task failed"
                        ) as error:
                            await execution.run(database, job.id)
                        assert "private" not in str(error.value)
            async with database.session() as session:
                assert (await session.get(Job, job.id)).status == JobStatus.FAILED
            # No record and already terminal deliveries are harmless.
            await execution.run(database, uuid4())
            await execution.run(database, job.id)

    asyncio.run(scenario())


def test_recovery_pages_and_terminal_queue_state(job_settings: Settings) -> None:
    """Active oldest work cannot starve later pages; terminal RQ failures stay visible."""
    settings = job_settings.model_copy(update={"job_batch_size": 1})

    async def scenario() -> None:
        async with Database(settings) as database:
            jobs = [await seed(database) for _ in range(2)]
            jobs.sort(key=lambda job: job.id)
            assert await queue.submit(database, settings, jobs[0].id)
            assert await queue.recover(database, settings) == 0
            assert await queue.recover(database, settings, jobs[0].id) == 1
            with queue.connection(settings) as client:
                rq_job = queue.queue_for(settings, client).fetch_job(str(jobs[0].id))
                rq_job.set_status(RQStatus.FAILED)
            assert await queue.recover(database, settings) == 0
            async with database.session() as session:
                assert (await session.get(Job, jobs[0].id)).status == JobStatus.FAILED

    asyncio.run(scenario())


def test_recurrence_and_invalid_entrypoint(
    job_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair recurrence at startup, retain it after ordinary failure, reject invalid IDs."""
    for key, value in environment(job_settings).items():
        if key.startswith("TINYPODS_"):
            monkeypatch.setenv(key, value)
    queue.schedule_reconciliation(job_settings)
    queue.schedule_reconciliation(job_settings)
    execution.reconcile()
    execution.reconcile(str(uuid4()))
    with (
        patch.object(execution, "bootstrap", new=AsyncMock(side_effect=RuntimeError("private"))),
        pytest.raises(execution.JobExecutionError),
    ):
        execution.reconcile()
    execution.execute(str(uuid4()))
    for identity in ("bad", uuid4().hex):
        with pytest.raises(ValueError):
            execution.execute(identity)
    with (
        pytest.raises(ConfigurationError, match="valkey"),
        queue.connection(Settings(valkey_url=None)),
    ):
        pass


def test_owner_job_status_api(job_settings: Settings) -> None:
    """Retain owner-only status reads without a diagnostic submission surface."""

    async def prepare() -> UUID:
        async with Database(job_settings) as database:
            await LocalPasswordProvider(database).bootstrap(credentials())
            return (await seed(database)).id

    identity = asyncio.run(prepare())
    with TestClient(create_app(job_settings), base_url="https://testserver") as client:
        path = "/api/v1/jobs/" + str(identity)
        assert client.get(path).status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            headers={"x-csrf-token": "login"},
            json={"username": "Owner", "password": credentials().password.get_secret_value()},
        )
        assert login.status_code == 200, login.text
        client.headers["x-csrf-token"] = client.cookies["__Host-tinypods-csrf"]
        result = client.get(path)
        assert result.status_code == 200
        assert result.json()["status"] == "pending"
        assert "data" not in result.json()
        assert client.get("/api/v1/jobs/" + str(uuid4())).status_code == 404
        assert client.post("/api/v1/jobs/database-check", json={}).status_code == 405
        schema = client.get("/openapi.json").json()
        assert "/api/v1/jobs/database-check" not in schema["paths"]
        assert "CheckRequest" not in schema["components"]["schemas"]


def test_real_noeviction_and_connection_outage(job_settings: Settings) -> None:
    """Full real Valkey rejects publication while PostgreSQL keeps the accepted Job."""

    async def scenario() -> None:
        async with Database(job_settings) as database:
            job = await seed(database)
            with queue.connection(job_settings) as client:
                previous = client.config_get("maxmemory")["maxmemory"]
                try:
                    client.config_set("maxmemory", 1)
                    assert not await queue.submit(database, job_settings, job.id)
                finally:
                    client.config_set("maxmemory", previous)
            offline = job_settings.model_copy(
                update={"valkey_url": SecretStr("redis://127.0.0.1:1")}
            )
            assert not await queue.submit(database, offline, job.id)
            assert await queue.submit(database, job_settings, job.id)

    asyncio.run(scenario())


def test_cancelled_execution_is_not_blindly_restarted(job_settings: Settings) -> None:
    """A task killed after claim leaves uncertain work for explicit failed-state review."""

    async def scenario() -> None:
        entered = asyncio.Event()

        async def interrupted(database: Database, inputs: tasks.TaskInput, job: Job) -> None:
            entered.set()
            await asyncio.Event().wait()

        async with Database(job_settings) as database:
            job = await seed(database)
            definition = replace(tasks.TASKS["tests.record"], run=interrupted)
            with patch.object(execution, "TASKS", {job.task: definition}):
                task = asyncio.create_task(execution.run(database, job.id))
                async with asyncio.timeout(5):
                    await entered.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            async with database.session() as session:
                assert (await session.get(Job, job.id)).status == JobStatus.RUNNING
            assert await queue.recover(database, job_settings) == 0
            async with database.session() as session:
                assert (await session.get(Job, job.id)).status == JobStatus.FAILED

    asyncio.run(scenario())


def test_payload_bounds_and_safe_task_resolution(job_settings: Settings) -> None:
    """Bound registered input snapshots and reject a forged DB task without importing it."""
    from tinypods_core.jobs import service

    class LargeInput(tasks.TaskInput):
        value: str

    async def scenario() -> None:
        async with Database(job_settings) as database:
            definition = replace(tasks.TASKS["tests.record"], schema=LargeInput)
            with patch.object(service, "TASKS", {"test.large": definition}):
                async with database.session() as session:
                    with pytest.raises(ValueError, match="16384"):
                        await create_job(session, "test.large", {"value": "x" * 17000})
            async with database.transaction() as session:
                user = User(username="related", is_owner=True)
                session.add(user)
                await session.flush()
                job = await create_job(
                    session,
                    "tests.record",
                    {},
                    requested_by_id=user.id,
                    target_type="auth.user",
                    target_id=user.id,
                )
            await execution.run(database, job.id)
            plain = await seed(database)
            await execution.run(database, plain.id)
            async with database.transaction() as session:
                forged = Job(task="os.system", data={"command": "private"})
                session.add(forged)
            with pytest.raises(execution.JobExecutionError):
                await execution.run(database, forged.id)

    asyncio.run(scenario())


def test_scheduler_orphan(job_settings: Settings) -> None:
    """Ignore an orphan scheduled ID when repairing recurrence."""
    with queue.connection(job_settings) as client:
        rq = queue.queue_for(job_settings, client)
        client.zadd(rq.scheduled_job_registry.key, {"orphan": datetime.now(UTC).timestamp() + 30})
    queue.schedule_reconciliation(job_settings)


def test_duplicate_delivery_during_execution(job_settings: Settings) -> None:
    """A concurrent duplicate cannot invoke a handler already claimed by another delivery."""

    async def scenario() -> None:
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def controlled(database: Database, inputs: tasks.TaskInput, job: Job) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await finish.wait()

        async with Database(job_settings) as database:
            job = await seed(database)
            definition = replace(tasks.TASKS[job.task], run=controlled)
            with patch.object(execution, "TASKS", {job.task: definition}):
                first = asyncio.create_task(execution.run(database, job.id))
                async with asyncio.timeout(5):
                    await entered.wait()
                    await execution.run(database, job.id)
                    finish.set()
                    await first
            assert calls == 1

    asyncio.run(scenario())
