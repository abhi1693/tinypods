"""Request-scoped dependencies; services choose when to commit a write."""

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.db import Database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-owned async session from the configured application database.

    Args:
        request: Current request carrying middleware-owned correlation state.

    Yields:
        A request-owned session; uncommitted work is rolled back on exit.

    Raises:
        HTTPException: Database access is not configured for this application.
    """
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    async with database.session() as session:
        yield session
