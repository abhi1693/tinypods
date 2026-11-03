"""Test-only task registration for in-process, subprocess and Compose RQ checks."""

from tinypods_core.db import Database
from tinypods_core.jobs import Job, execution, queue, service, tasks

TEST_TASK = "tests.record"


async def record(database: Database, inputs: tasks.TaskInput, job: Job) -> None:
    """Prove a test handler can use its own database lifetime after the worker claim."""
    await database.installation()


TEST_TASKS = {TEST_TASK: tasks.Task(tasks.TaskInput, record)}


def register() -> None:
    """Install only test-owned contracts in this isolated worker/test process."""
    tasks.TASKS = TEST_TASKS
    service.TASKS = TEST_TASKS
    queue.TASKS = TEST_TASKS
    execution.TASKS = TEST_TASKS


if __name__ == "__main__":
    from tinypods_worker.main import main

    register()
    main()
