"""Compose installation services from existing shared infrastructure."""

from typing import Annotated

from fastapi import Depends, Request
from tinypods_api.auth import AuthorizationDependency
from tinypods_api.http.dependencies import DatabaseDependency, Session
from tinypods_core.applications.installation_service import InstallationService
from tinypods_core.repositories.verification import ChartVerification
from tinypods_core.secrets.applications import ApplicationSecretService
from tinypods_core.secrets.providers import encryption_provider


def installations(
    request: Request,
    database: DatabaseDependency,
    authorization: AuthorizationDependency,
) -> InstallationService:
    """Borrow application resources; each service operation owns short sessions."""
    return InstallationService(
        database,
        authorization,
        ChartVerification(database, authorization, request.app.state.repository_client),
        request.app.state.installation_validator,
        encryption_provider(request.app.state.settings),
    )


def secrets(
    request: Request,
    session: Session,
    authorization: AuthorizationDependency,
) -> ApplicationSecretService:
    """Use the existing encrypted store in an authorized installation scope."""
    return ApplicationSecretService(
        session, authorization, encryption_provider(request.app.state.settings)
    )


InstallationDependency = Annotated[InstallationService, Depends(installations)]
SecretDependency = Annotated[ApplicationSecretService, Depends(secrets)]
