"""Compose repository metadata services using the shared request/auth boundaries."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from tinypods_api.applications.dependencies import Session
from tinypods_api.auth import SessionDependency
from tinypods_core.applications.service import DefinitionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.client import RepositoryClient
from tinypods_core.repositories.service import RepositorySync
from tinypods_core.repositories.transport import MetadataHTTP


def repositories(session: Session, auth: SessionDependency) -> DefinitionService[ChartRepository]:
    """Inject the source CRUD service without network I/O."""
    return DefinitionService(ChartRepository, session, AuthorizationService(auth))


def charts(session: Session, auth: SessionDependency) -> DefinitionService[Chart]:
    """Inject chart registration and browsing."""
    return DefinitionService(Chart, session, AuthorizationService(auth))


def versions(session: Session, auth: SessionDependency) -> DefinitionService[ChartVersion]:
    """Inject read-only synchronized version browsing."""
    return DefinitionService(ChartVersion, session, AuthorizationService(auth))


def sync(request: Request, auth: SessionDependency) -> RepositorySync:
    """Own no client pool; the bounded reader closes every request's transport."""
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    return RepositorySync(
        database,
        AuthorizationService(auth),
        RepositoryClient(MetadataHTTP(request.app.state.settings)),
    )


SyncDependency = Annotated[RepositorySync, Depends(sync)]
