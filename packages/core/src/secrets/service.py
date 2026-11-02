"""Owner-only credential lifecycle, scoped resolution and resumable key rotation."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import anyio
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import QueryableAttribute, undefer
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.sql.expression import SelectOfScalar
from tinypods_core.applications.installations import ApplicationInstallation, ApplicationSecret
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.clusters import Cluster
from tinypods_core.secrets.crypto import Ciphertext, SecretContext, SecretError
from tinypods_core.secrets.kubernetes import ConnectionResult, Kubeconfig, KubernetesClient
from tinypods_core.secrets.models import ClusterSecret, Secret, SecretVersion
from tinypods_core.secrets.providers import EncryptionProvider


@dataclass(frozen=True)
class RotationResult:
    """Report committed progress; skipped locked rows remain in the remaining count."""

    updated: int
    remaining: int
    active_key_id: str


class SecretService:
    """Own one request's session and keep confidential payloads inside core methods."""

    def __init__(
        self,
        session: AsyncSession,
        authorization: AuthorizationService,
        provider: EncryptionProvider,
        client: KubernetesClient | None = None,
    ) -> None:
        """Inject explicit authorization, external keys and an async cluster client."""
        self.session = session
        self.authorization = authorization
        self.provider = provider
        self.client = client if client is not None else KubernetesClient()

    async def authorize(self, principal: Principal) -> None:
        """Cluster credentials are platform-only, including metadata and history."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)

    async def _commit(self) -> None:
        """Translate scope/uniqueness conflicts without SQL or payload details."""
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise SecretError("secret_conflict", 409) from None

    async def _page[Record: AuditTrailMixin](
        self, query: SelectOfScalar[Record], limit: int, offset: int
    ) -> tuple[list[Record], int]:
        """Bound internal callers as well as HTTP and return a deterministic page."""
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid secret page")
        total = (
            await self.session.exec(
                select(func.count()).select_from(query.order_by(None).subquery())
            )
        ).one()
        rows = list((await self.session.exec(query.limit(limit).offset(offset))).all())
        return rows, total

    async def bindings(
        self, principal: Principal, *, limit: int, offset: int, cluster_id: UUID | None = None
    ) -> tuple[list[ClusterSecret], int]:
        """Browse safe cluster-secret metadata after owner authorization."""
        await self.authorize(principal)
        query = select(ClusterSecret).order_by(col(ClusterSecret.created), col(ClusterSecret.id))
        if cluster_id is not None:
            query = query.where(col(ClusterSecret.cluster_id) == cluster_id)
        return await self._page(query, limit, offset)

    async def versions(
        self, principal: Principal, *, limit: int, offset: int, secret_id: UUID | None = None
    ) -> tuple[list[SecretVersion], int]:
        """Browse version metadata; confidential columns stay deferred."""
        await self.authorize(principal)
        query = select(SecretVersion).order_by(col(SecretVersion.created), col(SecretVersion.id))
        if secret_id is not None:
            query = query.where(col(SecretVersion.secret_id) == secret_id)
        return await self._page(query, limit, offset)

    async def get[Record: AuditTrailMixin](
        self, principal: Principal, model: type[Record], identity: UUID
    ) -> Record:
        """Read safe resource metadata without resolving a confidential payload."""
        await self.authorize(principal)
        record = await self.session.get(model, identity)
        if record is None:
            raise SecretError("secret_not_found", 404)
        return record

    async def _version(
        self, secret_id: UUID, cluster_id: UUID, number: int, payload: bytes
    ) -> SecretVersion:
        """Validate the consumer format, then encrypt original bytes as a new version."""
        await anyio.to_thread.run_sync(Kubeconfig.parse(payload).tls_context)
        identity = uuid4()
        encrypted = await self.provider.encrypt(
            payload, SecretContext(secret_id, identity, number, cluster_id)
        )
        return SecretVersion(
            id=identity,
            secret_id=secret_id,
            version=number,
            encryption_key_id=encrypted.key_id,
            nonce=encrypted.nonce,
            encrypted_payload=encrypted.payload,
        )

    async def create(
        self,
        principal: Principal,
        cluster_id: UUID,
        name: str,
        description: str,
        payload: bytes,
        *,
        activate: bool = True,
    ) -> ClusterSecret:
        """Create complete metadata/version/scope atomically; serialize competing creates."""
        await self.authorize(principal)
        cluster = (
            await self.session.exec(
                select(Cluster).where(col(Cluster.id) == cluster_id).with_for_update()
            )
        ).first()
        if cluster is None:
            raise SecretError("cluster_not_found", 404)
        if (
            await self.session.exec(
                select(ClusterSecret.id).where(col(ClusterSecret.cluster_id) == cluster_id)
            )
        ).first() is not None:
            raise SecretError("secret_conflict", 409)
        secret = Secret(name=name, description=description)
        version = await self._version(secret.id, cluster_id, 1, payload)
        binding = ClusterSecret(
            secret_id=secret.id,
            cluster_id=cluster_id,
            active_version_id=version.id if activate else None,
        )
        self.session.add(secret)
        await self.session.flush()
        self.session.add(version)
        await self.session.flush()
        self.session.add(binding)
        await self._commit()
        return binding

    async def _locked_scope(self, secret_id: UUID) -> ClusterSecret:
        """Lock the shared secret before its association for every lifecycle writer.

        This order serializes version allocation, activation and revocation without
        holding a database row lock during an external Kubernetes request.
        """
        secret = (
            await self.session.exec(
                select(Secret).where(col(Secret.id) == secret_id).with_for_update()
            )
        ).first()
        if secret is None:
            raise SecretError("secret_not_found", 404)
        binding = (
            await self.session.exec(
                select(ClusterSecret)
                .where(col(ClusterSecret.secret_id) == secret_id)
                .with_for_update()
            )
        ).first()
        if binding is None:
            raise SecretError("secret_scope_missing", 409)
        return binding

    async def replace(
        self, principal: Principal, secret_id: UUID, payload: bytes, *, activate: bool = True
    ) -> SecretVersion:
        """Append and activate a new logical version; never overwrite an old payload."""
        await self.authorize(principal)
        binding = await self._locked_scope(secret_id)
        number = (
            await self.session.exec(
                select(func.max(SecretVersion.version)).where(
                    col(SecretVersion.secret_id) == secret_id
                )
            )
        ).one() or 0
        version = await self._version(secret_id, binding.cluster_id, number + 1, payload)
        self.session.add(version)
        await self.session.flush()
        if activate:
            binding.active_version_id = version.id
            self.session.add(binding)
        await self._commit()
        return version

    async def activate(
        self, principal: Principal, identity: UUID, version_id: UUID
    ) -> ClusterSecret:
        """Select a non-revoked version in this exact secret's cluster scope."""
        await self.authorize(principal)
        existing = await self.session.get(ClusterSecret, identity)
        if existing is None:
            raise SecretError("secret_not_found", 404)
        binding = await self._locked_scope(existing.secret_id)
        version = await self._payload_version(version_id)
        await self._resolve(version, binding)
        binding.active_version_id = version.id
        self.session.add(binding)
        await self._commit()
        return binding

    async def revoke(self, principal: Principal, version_id: UUID) -> SecretVersion:
        """Revoke future resolution without silently falling back to another version."""
        await self.authorize(principal)
        existing = await self.session.get(SecretVersion, version_id)
        if existing is None:
            raise SecretError("secret_not_found", 404)
        await self._locked_scope(existing.secret_id)
        # A waiting revoker must see the preceding committed change, not its
        # earlier identity-map snapshot. Revocation is idempotent and permanent.
        await self.session.refresh(existing, with_for_update=True)
        if existing.revoked_at is None:
            existing.revoked_at = datetime.now(UTC)
            self.session.add(existing)
        await self._commit()
        return existing

    async def _payload_version(self, identity: UUID) -> SecretVersion:
        """Load encrypted columns only at an explicit backend resolution boundary."""
        version = (
            await self.session.exec(
                select(SecretVersion)
                .where(col(SecretVersion.id) == identity)
                .options(
                    undefer(cast(QueryableAttribute[bytes], SecretVersion.nonce)),
                    undefer(cast(QueryableAttribute[bytes], SecretVersion.encrypted_payload)),
                )
                .execution_options(populate_existing=True)
            )
        ).first()
        if version is None:
            raise SecretError("secret_not_found", 404)
        return version

    async def _resolve(self, version: SecretVersion, binding: ClusterSecret) -> bytes:
        """Authenticate scope and revocation before decrypting any original bytes."""
        if version.secret_id != binding.secret_id:
            raise SecretError("secret_scope_mismatch", 409)
        if version.revoked_at is not None:
            raise SecretError("secret_revoked", 409)
        context = SecretContext(version.secret_id, version.id, version.version, binding.cluster_id)
        return await self.provider.decrypt(
            Ciphertext(version.encryption_key_id, version.nonce, version.encrypted_payload), context
        )

    async def test(self, principal: Principal, cluster_id: UUID) -> ConnectionResult:
        """Resolve the active cluster credential, then release the DB transaction before I/O."""
        await self.authorize(principal)
        binding = (
            await self.session.exec(
                select(ClusterSecret).where(col(ClusterSecret.cluster_id) == cluster_id)
            )
        ).first()
        if binding is None or binding.active_version_id is None:
            raise SecretError("secret_not_found", 404)
        version = await self._payload_version(binding.active_version_id)
        config = Kubeconfig.parse(await self._resolve(version, binding))
        await self.session.rollback()
        await self.authorize(principal)
        return await self.client.test(config)

    async def rotate(self, principal: Principal, batch_size: int = 100) -> RotationResult:
        """Re-encrypt one atomic resumable batch; concurrent writes use the active key.

        Locked rows are skipped to avoid waiting behind another operator. They
        remain in the final count, so a partial pass never authorizes key retirement.
        Old keys must remain configured until all ciphertext and recoverable backups
        no longer require them. Credential validity at Kubernetes is unchanged.
        """
        await self.authorize(principal)
        active = self.provider.active
        if active is None:
            raise SecretError("secret_key_unavailable", 503)
        if not 1 <= batch_size <= 1000:
            raise ValueError("Invalid rotation batch")
        rows = (
            await self.session.exec(
                select(SecretVersion, ClusterSecret.cluster_id, ApplicationInstallation)
                .outerjoin(
                    ClusterSecret, col(ClusterSecret.secret_id) == col(SecretVersion.secret_id)
                )
                .outerjoin(
                    ApplicationSecret,
                    col(ApplicationSecret.secret_id) == col(SecretVersion.secret_id),
                )
                .outerjoin(
                    ApplicationInstallation,
                    col(ApplicationInstallation.id) == col(ApplicationSecret.installation_id),
                )
                .where(col(SecretVersion.encryption_key_id) != active)
                .order_by(col(SecretVersion.id))
                .limit(batch_size)
                .with_for_update(of=SecretVersion, skip_locked=True)
                .options(
                    undefer(cast(QueryableAttribute[bytes], SecretVersion.nonce)),
                    undefer(cast(QueryableAttribute[bytes], SecretVersion.encrypted_payload)),
                )
            )
        ).all()
        try:
            for version, cluster_id, installation in rows:
                if installation is not None:
                    context = SecretContext(
                        version.secret_id,
                        version.id,
                        version.version,
                        installation.cluster_id,
                        installation.id,
                        installation.namespace,
                    )
                elif cluster_id is not None:
                    context = SecretContext(
                        version.secret_id, version.id, version.version, cluster_id
                    )
                else:
                    raise SecretError("secret_scope_missing", 409)
                original = await self.provider.decrypt(
                    Ciphertext(version.encryption_key_id, version.nonce, version.encrypted_payload),
                    context,
                )
                encrypted = await self.provider.encrypt(original, context)
                version.encryption_key_id, version.nonce, version.encrypted_payload = (
                    encrypted.key_id,
                    encrypted.nonce,
                    encrypted.payload,
                )
                self.session.add(version)
        except SecretError:
            await self.session.rollback()
            raise
        await self._commit()
        remaining = (
            await self.session.exec(
                select(func.count())
                .select_from(SecretVersion)
                .where(col(SecretVersion.encryption_key_id) != active)
            )
        ).one()
        return RotationResult(len(rows), remaining, active)

    async def cluster_binding(self, principal: Principal, cluster_id: UUID) -> ClusterSecret:
        """Resolve credential ownership from the cluster path without client-supplied secrets."""
        await self.authorize(principal)
        if await self.session.get(Cluster, cluster_id) is None:
            raise SecretError("cluster_not_found", 404)
        binding = (
            await self.session.exec(
                select(ClusterSecret).where(col(ClusterSecret.cluster_id) == cluster_id)
            )
        ).first()
        if binding is None:
            raise SecretError("kubeconfig_not_found", 404)
        return binding

    async def save_cluster_version(
        self, principal: Principal, cluster_id: UUID, payload: bytes, *, activate: bool = True
    ) -> SecretVersion:
        """Upload initial/replacement kubeconfig bytes without exposing storage associations.

        The cluster row serializes competing first uploads. Existing lifecycle
        writers keep their secret-before-association lock ordering. Internal secret
        creation is atomic with version one; callers only manage cluster versions.
        """
        await self.authorize(principal)
        cluster = (
            await self.session.exec(
                select(Cluster).where(col(Cluster.id) == cluster_id).with_for_update()
            )
        ).first()
        if cluster is None:
            raise SecretError("cluster_not_found", 404)
        binding = (
            await self.session.exec(
                select(ClusterSecret).where(col(ClusterSecret.cluster_id) == cluster_id)
            )
        ).first()
        if binding is None:
            binding = await self.create(
                principal, cluster_id, "Kubernetes kubeconfig", "", payload, activate=activate
            )
            return (
                await self.session.exec(
                    select(SecretVersion).where(
                        col(SecretVersion.secret_id) == binding.secret_id,
                        col(SecretVersion.version) == 1,
                    )
                )
            ).one()
        return await self.replace(principal, binding.secret_id, payload, activate=activate)

    async def cluster_versions(
        self, principal: Principal, cluster_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[SecretVersion], int]:
        """Browse history belonging to the exact cluster named by the request."""
        binding = await self.cluster_binding(principal, cluster_id)
        return await self.versions(
            principal, limit=limit, offset=offset, secret_id=binding.secret_id
        )

    async def cluster_version(
        self, principal: Principal, cluster_id: UUID, version_id: UUID
    ) -> tuple[ClusterSecret, SecretVersion]:
        """Reject cross-cluster references before reads, activation or revocation."""
        binding = await self.cluster_binding(principal, cluster_id)
        version = await self.get(principal, SecretVersion, version_id)
        if version.secret_id != binding.secret_id:
            raise SecretError("kubeconfig_version_not_found", 404)
        return binding, version

    async def activate_cluster_version(
        self, principal: Principal, cluster_id: UUID, version_id: UUID
    ) -> ClusterSecret:
        """Activate an existing non-revoked version in this cluster only."""
        binding, _ = await self.cluster_version(principal, cluster_id, version_id)
        return await self.activate(principal, binding.id, version_id)

    async def revoke_cluster_version(
        self, principal: Principal, cluster_id: UUID, version_id: UUID
    ) -> SecretVersion:
        """Permanently revoke a version after resolving its actual cluster scope."""
        await self.cluster_version(principal, cluster_id, version_id)
        return await self.revoke(principal, version_id)
