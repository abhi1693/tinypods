"""Compose domain CRUD with the shared eager-response pipeline."""

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
from tinypods_core.applications.service import DefinitionService, MembershipService
from tinypods_core.auth.authorization import AuthorizationService


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
