"""Compose scoped credential administration with the shared request resources."""

from fastapi import Request
from tinypods_api.auth import AuthorizationDependency
from tinypods_api.http.dependencies import Session
from tinypods_core.secrets.providers import encryption_provider
from tinypods_core.secrets.service import SecretService


def secrets(
    request: Request, session: Session, authorization: AuthorizationDependency
) -> SecretService:
    """Inject operator configuration; key material never enters response DTOs."""
    return SecretService(session, authorization, encryption_provider(request.app.state.settings))
