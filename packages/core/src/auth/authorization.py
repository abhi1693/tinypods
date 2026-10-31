"""Credential-neutral resource policy with injected, fail-closed principal validation."""

from typing import Protocol
from uuid import UUID

from tinypods_core.auth.contracts import AuthError, Principal, Scope


class PrincipalValidator(Protocol):
    """Revalidate opaque credential context without requiring passwords or browser cookies."""

    async def validate_principal(self, principal: Principal) -> Principal:
        """Return current verified identity or reject expired/revoked/unavailable credentials."""
        ...


class AuthorizationService:
    """Apply ownership policy only after the configured authenticator revalidates identity."""

    def __init__(self, validator: PrincipalValidator) -> None:
        """Retain a trusted validator; session validation is today's supported implementation."""
        self.validator = validator

    async def require_access(
        self, principal: Principal | None, scope: Scope, *, owner_id: UUID | None = None
    ) -> Principal:
        """Require resource ownership or explicit catalog-owner privileges."""
        if principal is None:
            raise AuthError(401)
        current = await self.validator.validate_principal(principal)
        if scope == Scope.CATALOG_ADMIN:
            allowed = current.is_owner
        else:
            allowed = (
                scope in (Scope.APP, Scope.CLUSTER, Scope.SECRET) and owner_id == current.user_id
            )
        if not allowed:
            raise AuthError(403)
        return current
