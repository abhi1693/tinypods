"""Compose repository metadata services using the shared request/auth boundaries."""

from typing import Annotated

from fastapi import Depends, Request
from tinypods_api.auth import AuthorizationDependency
from tinypods_api.http.dependencies import DatabaseDependency, Session
from tinypods_api.http.presentation import Presenter, ResourceService
from tinypods_api.repositories.contracts import ChartRead, RepositoryRead, VersionRead
from tinypods_core.applications.service import DefinitionService
from tinypods_core.repositories import Chart, ChartRepository, ChartVersion
from tinypods_core.repositories.service import RepositorySync
from tinypods_core.repositories.verification import ChartVerification


def repositories(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[ChartRepository, RepositoryRead]:
    """Inject the source CRUD service without network I/O."""
    return ResourceService(
        DefinitionService(ChartRepository, session, authorization), RepositoryRead
    )


def charts(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[Chart, ChartRead]:
    """Inject chart registration and browsing."""
    return ResourceService(DefinitionService(Chart, session, authorization), ChartRead)


def versions(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[ChartVersion, VersionRead]:
    """Inject read-only synchronized version browsing."""
    return ResourceService(DefinitionService(ChartVersion, session, authorization), VersionRead)


def sync(
    request: Request, database: DatabaseDependency, authorization: AuthorizationDependency
) -> RepositorySync:
    """Borrow the application-owned reader; identical in-flight reads can share transport work."""
    return RepositorySync(
        database,
        authorization,
        request.app.state.repository_client,
    )


SyncDependency = Annotated[RepositorySync, Depends(sync)]


def presenter(session: Session, authorization: AuthorizationDependency) -> Presenter:
    """Compose action-result projection with the same access and loading rules."""
    return Presenter(session, authorization)


PresenterDependency = Annotated[Presenter, Depends(presenter)]


def verification(
    request: Request, database: DatabaseDependency, authorization: AuthorizationDependency
) -> ChartVerification:
    """Compose bounded chart verification independently of metadata synchronization."""
    return ChartVerification(
        database,
        authorization,
        request.app.state.repository_client,
    )


VerificationDependency = Annotated[ChartVerification, Depends(verification)]
