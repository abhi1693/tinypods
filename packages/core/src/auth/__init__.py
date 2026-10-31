"""Public core local authentication and service authorization contracts."""

from tinypods_core.auth.authorization import AuthorizationService, PrincipalValidator
from tinypods_core.auth.contracts import AuthError, Credentials, Login, Principal, Scope
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.auth.service import AuthService
from tinypods_core.auth.sessions import IdentityProvider, SessionService

__all__ = [
    "AuthError",
    "AuthorizationService",
    "PrincipalValidator",
    "AuthService",
    "Credentials",
    "Login",
    "LocalPasswordProvider",
    "Principal",
    "Scope",
    "IdentityProvider",
    "SessionService",
]
