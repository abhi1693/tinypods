"""Application scope on the existing encrypted/versioned secret store."""

from uuid import UUID, uuid4

import anyio
from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.applications.installations import ApplicationInstallation, ApplicationSecret
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.secrets.crypto import MAX_SECRET_BYTES, SecretContext, SecretError
from tinypods_core.secrets.models import Secret, SecretVersion
from tinypods_core.secrets.providers import EncryptionProvider
from tinypods_core.secrets.service import SecretService


class ApplicationSecretService(SecretService):
    """Reuse encryption, metadata, version numbering and revocation conventions."""

    def __init__(
        self,
        session: AsyncSession,
        authorization: AuthorizationService,
        provider: EncryptionProvider,
    ) -> None:
        """Reuse the operation's connection for credential and resource checks."""
        super().__init__(session, authorization.in_session(session), provider)

    async def installation(self, principal: Principal, identity: UUID) -> ApplicationInstallation:
        """Authorize using the real installation owner before accessing secrets."""
        record = await self.session.get(ApplicationInstallation, identity)
        if record is None:
            raise SecretError("installation_not_found", 404)
        await self.authorization.require_access(principal, Scope.SECRET, owner_id=record.owner_id)
        return record

    async def scope(
        self,
        principal: Principal,
        installation_id: UUID,
        secret_id: UUID,
        *,
        lock: bool = False,
    ) -> tuple[ApplicationInstallation, ApplicationSecret]:
        """Reject cross-installation secret IDs without resolving encrypted columns."""
        installation = await self.installation(principal, installation_id)
        query = select(ApplicationSecret).where(
            col(ApplicationSecret.installation_id) == installation_id,
            col(ApplicationSecret.secret_id) == secret_id,
        )
        if lock:
            # Match the existing lifecycle lock order: secret before association.
            await self.session.exec(
                select(Secret).where(col(Secret.id) == secret_id).with_for_update()
            )
            query = query.with_for_update()
        scope = (await self.session.exec(query)).first()
        if scope is None:
            raise SecretError("secret_not_found", 404)
        return installation, scope

    async def save_application_version(
        self,
        principal: Principal,
        installation_id: UUID,
        payload: bytes,
        *,
        secret_id: UUID | None = None,
        name: str = "Application secret",
    ) -> tuple[ApplicationSecret, SecretVersion]:
        """Create or append encrypted UTF-8 payloads atomically, with no active pointer."""
        if not payload or len(payload) > MAX_SECRET_BYTES:
            raise SecretError("invalid_secret_payload")
        try:
            payload.decode("utf-8")
        except UnicodeError:
            raise SecretError("invalid_secret_payload") from None
        try:
            installation = await self.installation(principal, installation_id)
            if secret_id is None:
                secret = Secret(name=name, kind="application")
                self.session.add(secret)
                await self.session.flush()
                scope = ApplicationSecret(secret_id=secret.id, installation_id=installation_id)
                self.session.add(scope)
                number = 1
            else:
                installation, scope = await self.scope(
                    principal, installation_id, secret_id, lock=True
                )
                number = (
                    (
                        await self.session.exec(
                            select(func.max(SecretVersion.version)).where(
                                col(SecretVersion.secret_id) == secret_id
                            )
                        )
                    ).one()
                    or 0
                ) + 1
            identity = uuid4()
            encrypted = await self.provider.encrypt(
                payload,
                SecretContext(
                    scope.secret_id,
                    identity,
                    number,
                    installation.cluster_id,
                    installation.id,
                    installation.namespace,
                ),
            )
            version = SecretVersion(
                id=identity,
                secret_id=scope.secret_id,
                version=number,
                encryption_key_id=encrypted.key_id,
                nonce=encrypted.nonce,
                encrypted_payload=encrypted.payload,
            )
            self.session.add(version)
            await self._commit()
            return scope, version
        except BaseException:
            # This service owns its writes. Roll back even when cancellation or
            # key-provider failure occurs before the commit boundary.
            with anyio.CancelScope(shield=True):
                await self.session.rollback()
            raise

    async def application_versions(
        self,
        principal: Principal,
        installation_id: UUID,
        secret_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[SecretVersion], int]:
        """Read safe exact-scope metadata without decrypting historical payloads."""
        await self.scope(principal, installation_id, secret_id)
        return await self._page(
            select(SecretVersion)
            .where(
                col(SecretVersion.secret_id) == secret_id,
            )
            .order_by(col(SecretVersion.version), col(SecretVersion.id)),
            limit,
            offset,
        )

    async def application_secrets(
        self,
        principal: Principal,
        installation_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[ApplicationSecret], int]:
        """List only secret identities attached to the authorized installation."""
        await self.installation(principal, installation_id)
        return await self._page(
            select(ApplicationSecret)
            .where(
                col(ApplicationSecret.installation_id) == installation_id,
            )
            .order_by(col(ApplicationSecret.created), col(ApplicationSecret.id)),
            limit,
            offset,
        )

    async def application_version(
        self,
        principal: Principal,
        installation_id: UUID,
        secret_id: UUID,
        version_id: UUID,
    ) -> SecretVersion:
        """Read metadata for one version in the requested actual scope."""
        await self.scope(principal, installation_id, secret_id)
        version = await self.session.get(SecretVersion, version_id)
        if version is None or version.secret_id != secret_id:
            raise SecretError("secret_not_found", 404)
        return version

    async def revoke_application_version(
        self,
        principal: Principal,
        installation_id: UUID,
        secret_id: UUID,
        version_id: UUID,
    ) -> SecretVersion:
        """Serialize permanent revocation with append and revision binding checks."""
        from datetime import UTC, datetime

        await self.scope(principal, installation_id, secret_id, lock=True)
        version = await self.application_version(principal, installation_id, secret_id, version_id)
        await self.session.refresh(version, with_for_update=True)
        if version.revoked_at is None:
            version.revoked_at = datetime.now(UTC)
            self.session.add(version)
        await self._commit()
        return version

    async def resolve_application_version(
        self,
        principal: Principal,
        installation_id: UUID,
        version_id: UUID,
    ) -> str:
        """Resolve confidential bytes only for bounded backend validation."""
        metadata = await self.session.get(SecretVersion, version_id)
        if metadata is None:
            raise SecretError("secret_not_found", 404)
        installation, scope = await self.scope(principal, installation_id, metadata.secret_id)
        version = await self._payload_version(version_id)
        if version.revoked_at is not None:
            raise SecretError("secret_revoked", 409)
        from tinypods_core.secrets.crypto import Ciphertext

        payload = await self.provider.decrypt(
            Ciphertext(version.encryption_key_id, version.nonce, version.encrypted_payload),
            SecretContext(
                scope.secret_id,
                version.id,
                version.version,
                installation.cluster_id,
                installation.id,
                installation.namespace,
            ),
        )
        try:
            return payload.decode("utf-8")
        except UnicodeError:
            raise SecretError("invalid_secret_payload") from None
