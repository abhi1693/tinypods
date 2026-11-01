"""Compose request-owned definition services from the shared database/auth boundaries."""

from typing import Annotated

from fastapi import Depends
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_api.auth import SessionDependency
from tinypods_api.http import get_session
from tinypods_core.applications import Application, Category, Release
from tinypods_core.applications.service import DefinitionService, MembershipService
from tinypods_core.auth.authorization import AuthorizationService

Session = Annotated[AsyncSession, Depends(get_session)]


def applications(session: Session, auth: SessionDependency) -> DefinitionService[Application]:
    """Inject the application service with a request-owned session."""
    return DefinitionService(Application, session, AuthorizationService(auth))


def categories(session: Session, auth: SessionDependency) -> DefinitionService[Category]:
    """Inject the category service with a request-owned session."""
    return DefinitionService(Category, session, AuthorizationService(auth))


def releases(session: Session, auth: SessionDependency) -> DefinitionService[Release]:
    """Inject the release service with a request-owned session."""
    return DefinitionService(Release, session, AuthorizationService(auth))


def memberships(session: Session, auth: SessionDependency) -> MembershipService:
    """Inject category membership operations using the same request boundaries."""
    return MembershipService(session, AuthorizationService(auth))
