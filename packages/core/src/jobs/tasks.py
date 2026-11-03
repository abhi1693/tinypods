"""Code-owned task/input and related-object contracts, never client import paths."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.db import Database
from tinypods_core.db.models import User
from tinypods_core.jobs.models import Job


class TaskInput(BaseModel):
    """Validate explicit safe task fields and reject arbitrary extra arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


@dataclass(frozen=True)
class Task:
    """Bind one trusted handler to its input schema and RQ execution policy."""

    schema: type[TaskInput]
    run: Callable[[Database, TaskInput, Job], Awaitable[None]]
    timeout: int = 60
    retry_intervals: tuple[int, ...] = (10, 30, 60)


async def check_database(database: Database, inputs: TaskInput, job: Job) -> None:
    """Run an operator-requested database diagnostic through the worker path."""
    if job.requested_by_id is not None:
        async with database.session() as session:
            actor = await session.get(User, job.requested_by_id)
            if actor is None or not actor.is_owner:
                raise ValueError("Job actor is no longer authorized")
    await database.installation()


async def user_exists(session: AsyncSession, identity: UUID) -> bool:
    """Resolve an allowed related object without dynamic model or SQL lookup."""
    return await session.get(User, identity) is not None


TASKS = MappingProxyType({"system.database_check": Task(TaskInput, check_database)})
TARGETS = MappingProxyType({"auth.user": user_exists})
