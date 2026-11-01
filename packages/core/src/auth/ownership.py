"""Trusted operator access management; no public privilege mutation endpoint."""

from uuid import UUID

from sqlmodel import select
from tinypods_core.auth.contracts import AuthError
from tinypods_core.db import Database
from tinypods_core.db.models import Installation, User


class OwnerAccess:
    """Manage instance owners through an audited, deployment-operator-only boundary."""

    def __init__(self, database: Database) -> None:
        """Retain the caller-owned database and its audit transaction machinery."""
        self.database = database

    async def set_owner(self, user_id: UUID, *, enabled: bool) -> None:
        """Grant or revoke owner access for an exact existing account.

        The trusted CLI supplies audit context. Current sessions resolve the stored
        account on every authorization check, so grants/revocations take effect there.
        The initial owner marker is historical and is never cleared on revocation.

        Args:
            user_id: Verified existing core account identifier, never an email match.
            enabled: Whether the account should have instance-wide administrative rights.

        Raises:
            AuthError: Missing instance (503), missing user (404), or an attempt to
                revoke the last owner (409).
        """
        async with self.database.transaction() as session:
            instance = (await session.exec(select(Installation).with_for_update())).one_or_none()
            if instance is None:
                raise AuthError(503)
            user = await session.get(User, user_id)
            if user is None:
                raise AuthError(404)
            if user.is_owner and not enabled:
                # Serialize all owner changes on the singleton before checking the
                # remaining owners, preventing concurrent revocations of the last two.
                other = (
                    await session.exec(
                        select(User.id).where(User.is_owner, User.id != user_id).limit(1)
                    )
                ).first()
                if other is None:
                    raise AuthError(409)
            user.is_owner = enabled
            session.add(user)
