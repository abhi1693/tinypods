"""Public HTTP authentication dependency and router composition boundary."""

from tinypods_api.auth.dependencies import (
    AuthDependency,
    PrincipalDependency,
    SessionDependency,
    get_auth,
    get_principal,
    get_sessions,
)
from tinypods_api.auth.routes import router

__all__ = [
    "AuthDependency",
    "PrincipalDependency",
    "SessionDependency",
    "get_sessions",
    "get_auth",
    "get_principal",
    "router",
]
