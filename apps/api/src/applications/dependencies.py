"""Compose domain CRUD and publication with shared authorization and eager responses."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from tinypods_api.applications.contracts import (
    ApplicationRead,
    CategoryRead,
    MembershipRead,
    ReleaseRead,
)
from tinypods_api.auth import SessionDependency
from tinypods_api.http.dependencies import Session
from tinypods_api.http.presentation import MembershipResource, ResourceService
from tinypods_core.applications import Application, Category, Release
from tinypods_core.applications.publication import ReleasePublicationService
from tinypods_core.applications.service import DefinitionService, MembershipService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.repositories.verification import ChartVerification


def applications(
    session: Session, auth: SessionDependency
) -> ResourceService[Application, ApplicationRead]:
    """Compose application metadata reads and audited writes."""
    return ResourceService(
        DefinitionService(Application, session, AuthorizationService(auth)), ApplicationRead
    )


def categories(
    session: Session, auth: SessionDependency
) -> ResourceService[Category, CategoryRead]:
    """Compose reusable category resources."""
    return ResourceService(
        DefinitionService(Category, session, AuthorizationService(auth)), CategoryRead
    )


def releases(session: Session, auth: SessionDependency) -> ResourceService[Release, ReleaseRead]:
    """Compose release resources with prefetched application and chart-version references."""
    return ResourceService(
        DefinitionService(Release, session, AuthorizationService(auth)), ReleaseRead
    )


def memberships(session: Session, auth: SessionDependency) -> MembershipResource[MembershipRead]:
    """Compose composite memberships without per-record related queries."""
    return MembershipResource(
        MembershipService(session, AuthorizationService(auth)), MembershipRead
    )


def publication_service(request: Request, auth: SessionDependency) -> ReleasePublicationService:
    """Compose publication from the existing app database and exact chart verifier."""
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503)
    authorization = AuthorizationService(auth)
    charts = ChartVerification(database, authorization, request.app.state.repository_client)
    return ReleasePublicationService(
        database, authorization, charts, request.app.state.publication_validator
    )


PublicationDependency = Annotated[ReleasePublicationService, Depends(publication_service)]
