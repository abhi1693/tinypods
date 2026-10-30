"""Async application sessions and direct, synchronous migration connections."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, Pool
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.models import Installation
from tinypods_core.schemas import InstallationInfo
from tinypods_core.settings import ConfigurationError, Settings


class PoolOptions(TypedDict, total=False):
    poolclass: type[Pool]
    pool_size: int
    max_overflow: int
    pool_timeout: float


def database_url(settings: Settings, *, migrations: bool = False) -> URL:
    configured = settings.database_url
    if migrations:
        configured = settings.migration_database_url
        if configured is None and settings.database_mode == "pgbouncer":
            raise ConfigurationError("A direct migration_database_url is required with PgBouncer")
        configured = configured or settings.database_url
    if configured is None:
        raise ConfigurationError("database_url is required for database operations")
    return make_url(configured.get_secret_value()).set(drivername="postgresql+psycopg")


def migration_engine(settings: Settings) -> Engine:
    """CLI-only engine: direct connection, no application pool or prepared statements."""
    return create_engine(
        database_url(settings, migrations=True),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.database_connect_timeout,
            "prepare_threshold": None,
        },
    )


class Database:
    """One async engine per process; each request/job owns its own session."""

    def __init__(self, settings: Settings) -> None:
        external_pool = settings.database_mode == "pgbouncer"
        options: PoolOptions = (
            {"poolclass": NullPool}
            if external_pool
            else {
                "pool_size": settings.database_pool_size,
                "max_overflow": settings.database_max_overflow,
                "pool_timeout": settings.database_pool_timeout,
            }
        )
        self.engine = create_async_engine(
            database_url(settings),
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": settings.database_connect_timeout,
                "prepare_threshold": None if external_pool else 5,
            },
            **options,
        )
        self._sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Read/request scope. Uncommitted work is rolled back on close."""
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Write scope. Commit on success; rollback on any exception."""
        async with self.session() as session, session.begin():
            yield session

    async def installation(self) -> InstallationInfo:
        async with self.session() as session:
            result = await session.exec(select(Installation).where(Installation.singleton == 1))
            return InstallationInfo.model_validate(result.one())

    async def ping(self) -> bool:
        try:
            await self.installation()
            return True
        except SQLAlchemyError:
            return False

    async def close(self) -> None:
        await self.engine.dispose()
