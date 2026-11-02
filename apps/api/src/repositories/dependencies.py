"""Compose repository metadata services using the shared request/auth boundaries."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from tinypods_api.auth import SessionDependency
from tinypods_api.http.dependencies import Session
from tinypods_api.http.presentation import Presenter, ResourceService
from tinypods_api.repositories.contracts import ChartRead, RepositoryRead, VersionRead
from tinypods_core.applications.service import DefinitionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.service import RepositorySync
from tinypods_core.repositories.verification import ChartVerification


def repositories(
    session: Session, auth: SessionDependency
) -> ResourceService[ChartRepository, RepositoryRead]:
    """Inject the source CRUD service without network I/O."""
    return ResourceService(
        DefinitionService(ChartRepository, session, AuthorizationService(auth)), RepositoryRead
    )


def charts(session: Session, auth: SessionDependency) -> ResourceService[Chart, ChartRead]:
    """Inject chart registration and browsing."""
    return ResourceService(DefinitionService(Chart, session, AuthorizationService(auth)), ChartRead)


def versions(
    session: Session, auth: SessionDependency
) -> ResourceService[ChartVersion, VersionRead]:
    """Inject read-only synchronized version browsing."""
    return ResourceService(
        DefinitionService(ChartVersion, session, AuthorizationService(auth)), VersionRead
    )


def sync(request: Request, auth: SessionDependency) -> RepositorySync:
    """Borrow the application-owned reader; identical in-flight reads can share transport work."""
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    return RepositorySync(
        database,
        AuthorizationService(auth),
        request.app.state.repository_client,
    )


SyncDependency = Annotated[RepositorySync, Depends(sync)]


def presenter(session: Session, auth: SessionDependency) -> Presenter:
    """Compose action-result projection with the same access and loading rules."""
    return Presenter(session, AuthorizationService(auth))


PresenterDependency = Annotated[Presenter, Depends(presenter)]


def verification(request: Request, auth: SessionDependency) -> ChartVerification:
    """Compose bounded chart verification independently of metadata synchronization."""
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    return ChartVerification(
        database,
        AuthorizationService(auth),
        request.app.state.repository_client,
    )


VerificationDependency = Annotated[ChartVerification, Depends(verification)]
