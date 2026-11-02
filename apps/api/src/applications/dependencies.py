"""Compose domain CRUD and publication with shared authorization and eager responses."""

from typing import Annotated

from fastapi import Depends, Request
from tinypods_api.applications.contracts import (
    ApplicationRead,
    CategoryRead,
    MembershipRead,
    ReleaseRead,
)
from tinypods_api.auth import AuthorizationDependency
from tinypods_api.http.dependencies import DatabaseDependency, Session
from tinypods_api.http.presentation import MembershipResource, ResourceService
from tinypods_core.applications import Application, Category, Release
from tinypods_core.applications.publication import ReleasePublicationService
from tinypods_core.applications.service import DefinitionService, MembershipService
from tinypods_core.repositories.verification import ChartVerification


def applications(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[Application, ApplicationRead]:
    """Compose application metadata reads and audited writes."""
    return ResourceService(DefinitionService(Application, session, authorization), ApplicationRead)


def categories(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[Category, CategoryRead]:
    """Compose reusable category resources."""
    return ResourceService(DefinitionService(Category, session, authorization), CategoryRead)


def releases(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[Release, ReleaseRead]:
    """Compose release resources with prefetched application and chart-version references."""
    return ResourceService(DefinitionService(Release, session, authorization), ReleaseRead)


def memberships(
    session: Session, authorization: AuthorizationDependency
) -> MembershipResource[MembershipRead]:
    """Compose composite memberships without per-record related queries."""
    return MembershipResource(MembershipService(session, authorization), MembershipRead)


def publication_service(
    request: Request, database: DatabaseDependency, authorization: AuthorizationDependency
) -> ReleasePublicationService:
    """Compose publication from the existing app database and exact chart verifier."""
    charts = ChartVerification(database, authorization, request.app.state.repository_client)
    return ReleasePublicationService(
        database, authorization, charts, request.app.state.publication_validator
    )


PublicationDependency = Annotated[ReleasePublicationService, Depends(publication_service)]
