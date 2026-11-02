"""Shared dependency lifetimes and fail-closed availability across feature graphs."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_api.applications.contracts import ApplicationRead
from tinypods_api.applications.dependencies import applications, publication_service
from tinypods_api.http.dependencies import get_database
from tinypods_api.http.presentation import ResourceService
from tinypods_api.main import create_app
from tinypods_api.repositories.contracts import ChartRead
from tinypods_api.repositories.dependencies import charts, sync, verification
from tinypods_core.applications import Application
from tinypods_core.repositories import Chart
from tinypods_core.settings import Settings


@pytest.mark.parametrize("provider", [applications, sync, verification, publication_service])
def test_missing_database_uses_one_overridable_boundary(provider: Callable[..., object]) -> None:
    """Exercise feature dependency graphs without the separate endpoint-rate-limit gate."""
    app = create_app(Settings())

    @app.get("/dependency-probe")
    async def probe(service: Annotated[object, Depends(provider)]) -> dict[str, bool]:
        raise AssertionError("A missing database must never reach the feature")

    with TestClient(app) as client:
        response = client.get("/dependency-probe")
        assert response.status_code == 503
        assert response.json()["code"] == "http_503"
        assert response.json()["message"] == "Service Unavailable"

        def unavailable() -> None:
            raise HTTPException(418)

        app.dependency_overrides[get_database] = unavailable
        assert client.get("/dependency-probe").status_code == 418


def test_shared_request_resources_do_not_probe_or_close_borrowed_database() -> None:
    """Reuse session/policy within a request, isolate requests and clean up after failure."""

    class BorrowedDatabase:
        opened = 0
        closed = 0

        @asynccontextmanager
        async def session(self) -> AsyncIterator[AsyncSession]:
            self.opened += 1
            try:
                async with AsyncSession() as session:
                    yield session
            finally:
                self.closed += 1

        async def ping(self) -> bool:
            raise AssertionError("Dependencies must not ping per request")

        async def close(self) -> None:
            raise AssertionError("Request dependencies do not own the database")

    database = BorrowedDatabase()
    app = create_app(Settings())
    app.state.database = database
    validator = object()
    app.state.sessions = validator
    resources = []

    @app.get("/dependency-probe")
    async def probe(
        first: Annotated[ResourceService[Application, ApplicationRead], Depends(applications)],
        second: Annotated[ResourceService[Chart, ChartRead], Depends(charts)],
        fail: bool = False,
    ) -> dict[str, bool]:
        assert first.core.session is second.core.session
        assert first.core.authorization is second.core.authorization
        assert first.core.authorization.validator is validator
        resources.append((first.core.session, first.core.authorization))
        if fail:
            raise ValueError("programming error must not become unavailable")
        return {"shared": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/dependency-probe").json() == {"shared": True}
        assert client.get("/dependency-probe").status_code == 200
        assert resources[0][0] is not resources[1][0]
        assert resources[0][1] is not resources[1][1]
        assert client.get("/dependency-probe?fail=true").status_code == 500
        assert database.opened == database.closed == 3
