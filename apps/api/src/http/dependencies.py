"""Request-scoped dependencies; services choose when to commit a write."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.db import Database


def get_database(request: Request) -> Database:
    """Borrow the configured database owner without opening a connection or probing it.

    Args:
        request: Current request carrying application-owned runtime resources.

    Returns:
        The configured database owner; its caller chooses the session lifetime.

    Raises:
        HTTPException: Database access is not configured for this application.
    """
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    return database


DatabaseDependency = Annotated[Database, Depends(get_database)]


async def get_session(database: DatabaseDependency) -> AsyncIterator[AsyncSession]:
    """Yield a request-owned async session from the configured application database.

    Args:
        database: Shared database owner resolved without a per-request readiness probe.

    Yields:
        A request-owned session; uncommitted work is rolled back on exit.

    Raises:
        HTTPException: Database access is not configured for this application.
    """
    async with database.session() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
