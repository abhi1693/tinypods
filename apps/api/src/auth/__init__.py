"""Public HTTP authentication dependency and router composition boundary."""

from tinypods_api.auth.dependencies import (
    AuthDependency,
    AuthorizationDependency,
    PrincipalDependency,
    SessionDependency,
    get_auth,
    get_authorization,
    get_principal,
    get_sessions,
)
from tinypods_api.auth.routes import build_router, router

__all__ = [
    "AuthDependency",
    "AuthorizationDependency",
    "get_authorization",
    "PrincipalDependency",
    "SessionDependency",
    "get_sessions",
    "get_auth",
    "get_principal",
    "router",
    "build_router",
]
