"""Authorize intended targets and append validated immutable configuration atomically."""

import asyncio
import copy
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core import __version__
from tinypods_core.applications.installation_contracts import KubernetesSecretBinding, SecretBinding
from tinypods_core.applications.installation_validation import (
    InstallationConfiguration,
    InstallationValidator,
)
from tinypods_core.applications.installations import (
    ApplicationInstallation,
    ApplicationSecret,
    InstallationRevision,
    RevisionKubernetesSecret,
    RevisionSecret,
)
from tinypods_core.applications.models import Application, Release
from tinypods_core.applications.publication import PublicationFailure
from tinypods_core.applications.service import DefinitionError, validate_page
from tinypods_core.applications.validation import ReleaseDraft
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.clusters import Cluster
from tinypods_core.db import Database
from tinypods_core.json_types import JsonRecord
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.verification import ChartVerification
from tinypods_core.secrets.crypto import Ciphertext, SecretContext, SecretError
from tinypods_core.secrets.models import Secret, SecretVersion
from tinypods_core.secrets.providers import EncryptionProvider


class InstallationService:
    """Keep external work outside DB lifetimes and serialize each history append."""

    def __init__(
        self,
        database: Database,
        authorization: AuthorizationService,
        charts: ChartVerification,
        validator: InstallationValidator,
        provider: EncryptionProvider,
    ) -> None:
        """Borrow application-owned infrastructure; sessions belong to each operation."""
        self.database, self.authorization, self.charts = database, authorization, charts
        self.validator, self.provider = validator, provider

    async def _installation(
        self,
        session: AsyncSession,
        principal: Principal,
        identity: UUID,
        *,
        lock: bool = False,
    ) -> ApplicationInstallation:
        """Revalidate the stored owner; a platform owner cannot cross resource ownership."""
        query = select(ApplicationInstallation).where(col(ApplicationInstallation.id) == identity)
        if lock:
            query = query.with_for_update()
        record = (await session.exec(query)).one_or_none()
        if record is None:
            raise DefinitionError(404)
        await self.authorization.in_session(session).require_access(
            principal, Scope.APP, owner_id=record.owner_id
        )
        return record

    async def create(
        self,
        principal: Principal,
        name: str,
        cluster_id: UUID,
        namespace: str,
    ) -> ApplicationInstallation:
        """Reserve an intended target using the platform's current cluster access policy."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        if not name.strip() or len(name) > 255:
            raise DefinitionError(422)
        try:
            async with self.database.transaction() as session:
                if await session.get(Cluster, cluster_id) is None:
                    raise DefinitionError(404)
                record = ApplicationInstallation(
                    name=name,
                    owner_id=principal.user_id,
                    cluster_id=cluster_id,
                    namespace=namespace,
                )
                session.add(record)
                await session.flush()
            return record
        except IntegrityError:
            # Translate FK/constraint races at this service-owned write boundary;
            # the transaction context has already rolled back rows and audit.
            raise DefinitionError(409) from None

    async def get(self, principal: Principal, identity: UUID) -> ApplicationInstallation:
        """Read saved metadata without downloading charts or contacting Kubernetes."""
        async with self.database.session() as session:
            return await self._installation(session, principal, identity)

    async def browse(
        self,
        principal: Principal,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[ApplicationInstallation], int]:
        """List only this principal's installations in deterministic order."""
        current = await self.authorization.validator.validate_principal(principal)
        validate_page(limit, offset)
        async with self.database.session() as session:
            query = select(ApplicationInstallation).where(
                col(ApplicationInstallation.owner_id) == current.user_id
            )
            total = (await session.exec(select(func.count()).select_from(query.subquery()))).one()
            rows = (
                await session.exec(
                    query.order_by(
                        col(ApplicationInstallation.created), col(ApplicationInstallation.id)
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return list(rows), total

    async def rename(
        self, principal: Principal, identity: UUID, name: str
    ) -> ApplicationInstallation:
        """Edit only the user-facing description/name; target and deployed pointer stay fixed."""
        if not name.strip() or len(name) > 255:
            raise DefinitionError(422)
        async with self.database.transaction() as session:
            record = await self._installation(session, principal, identity, lock=True)
            record.name = name
            session.add(record)
        return record

    async def history(
        self,
        principal: Principal,
        identity: UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[InstallationRevision], int]:
        """Read immutable configuration history newest first, independently of upstream state."""
        validate_page(limit, offset)
        async with self.database.session() as session:
            await self._installation(session, principal, identity)
            query = select(InstallationRevision).where(
                col(InstallationRevision.installation_id) == identity
            )
            total = (await session.exec(select(func.count()).select_from(query.subquery()))).one()
            rows = (
                await session.exec(
                    query.order_by(
                        col(InstallationRevision.created).desc(),
                        col(InstallationRevision.id).desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return list(rows), total

    async def revision(
        self, principal: Principal, installation_id: UUID, identity: UUID
    ) -> InstallationRevision:
        """Reject cross-installation IDs before returning saved configuration."""
        async with self.database.session() as session:
            await self._installation(session, principal, installation_id)
            record = await session.get(InstallationRevision, identity)
            if record is None or record.installation_id != installation_id:
                raise DefinitionError(404)
            return record

    async def _release(
        self, session: AsyncSession, principal: Principal, identity: UUID
    ) -> Release:
        """Lock eligibility through the final transaction, including application visibility."""
        record = (
            await session.exec(
                select(Release).where(col(Release.id) == identity).with_for_update(read=True)
            )
        ).one_or_none()
        if record is None:
            raise DefinitionError(404)
        app = (
            await session.exec(
                select(Application)
                .where(col(Application.id) == record.application_id)
                .with_for_update(read=True)
            )
        ).one()
        if (
            record.state != "published"
            or record.revoked_at is not None
            or (not principal.is_owner and app.state != "published")
        ):
            raise DefinitionError(409)
        return record

    async def _capture(
        self, principal: Principal, installation_id: UUID, release_id: UUID
    ) -> ReleaseDraft:
        """Detach immutable release fields, then obtain the chart pin in its own short session."""
        async with self.database.session() as session:
            await self._installation(session, principal, installation_id)
            release = await self._release(session, principal, release_id)
            fields = copy.deepcopy(release.configuration_fields)
            application_id, version_id, revision = (
                release.application_id,
                release.chart_version_id,
                release.revision,
            )
            options: JsonRecord = {
                "architectures": list(release.architectures),
                "renderer": release.renderer,
                "renderer_contract": release.renderer_contract,
                "core_version_min": release.core_version_min,
                "core_version_max": release.core_version_max,
            }
        pin = await self.charts.pin(version_id)
        return ReleaseDraft(
            release_id, application_id, version_id, revision, fields, options, __version__, pin
        )

    async def _bindings(
        self,
        session: AsyncSession,
        installation: ApplicationInstallation,
        secrets: list[SecretBinding],
        kubernetes_secrets: list[KubernetesSecretBinding],
        *,
        load_payloads: bool,
    ) -> dict[str, tuple[Ciphertext, SecretContext]]:
        """Check references and optionally detach encrypted values in one bounded query.

        The caller has already authorized the installation in this transaction.
        load_payloads is only for the pre-validation snapshot; final commit
        checks load metadata only and cannot decrypt confidential bytes.
        """
        targets = [item.target for item in secrets] + [item.target for item in kubernetes_secrets]
        if len(targets) > 100 or len(set(targets)) != len(targets):
            raise DefinitionError(422)
        if any(item.namespace != installation.namespace for item in kubernetes_secrets):
            raise DefinitionError(422)
        if not secrets:
            return {}
        query = (
            select(SecretVersion)
            .join(
                ApplicationSecret, col(ApplicationSecret.secret_id) == col(SecretVersion.secret_id)
            )
            .join(Secret, col(Secret.id) == col(SecretVersion.secret_id))
            .where(
                col(SecretVersion.id).in_([binding.version_id for binding in secrets]),
                col(ApplicationSecret.installation_id) == installation.id,
                col(Secret.kind) == "application",
            )
            .order_by(col(SecretVersion.id))
            .with_for_update(read=True, of=SecretVersion)
            .execution_options(populate_existing=True)
        )
        if load_payloads:
            query = query.options(undefer("*"))
        versions = {version.id: version for version in (await session.exec(query)).all()}
        if any(
            binding.version_id not in versions
            or versions[binding.version_id].revoked_at is not None
            for binding in secrets
        ):
            raise DefinitionError(409)
        if not load_payloads:
            return {}
        # Copy encrypted bytes while eligibility/key-rotation locks are held.
        # Decryption (including async key-file reads) happens after session exit.
        return {
            binding.target: (
                Ciphertext(
                    versions[binding.version_id].encryption_key_id,
                    versions[binding.version_id].nonce,
                    versions[binding.version_id].encrypted_payload,
                ),
                SecretContext(
                    versions[binding.version_id].secret_id,
                    binding.version_id,
                    versions[binding.version_id].version,
                    installation.cluster_id,
                    installation.id,
                    installation.namespace,
                ),
            )
            for binding in secrets
        }

    async def _confidential_values(
        self,
        bindings: dict[str, tuple[Ciphertext, SecretContext]],
    ) -> dict[str, str]:
        """Decrypt detached exact versions without retaining a database connection."""
        values: dict[str, str] = {}
        for target, (ciphertext, context) in bindings.items():
            payload = await self.provider.decrypt(ciphertext, context)
            try:
                values[target] = payload.decode("utf-8")
            except UnicodeError:
                raise SecretError("invalid_secret_payload") from None
        return values

    async def prepare(
        self,
        principal: Principal,
        installation_id: UUID,
        release_id: UUID,
        overrides: JsonRecord,
        secrets: list[SecretBinding],
        kubernetes_secrets: list[KubernetesSecretBinding],
    ) -> tuple[ReleaseDraft, InstallationConfiguration]:
        """Release all DB connections before chart downloads and bounded worker execution."""
        draft = await self._capture(principal, installation_id, release_id)
        async with self.database.session() as session:
            installation = await self._installation(session, principal, installation_id)
            encrypted = await self._bindings(
                session, installation, secrets, kubernetes_secrets, load_payloads=True
            )
        confidential = await self._confidential_values(encrypted)
        try:
            # Admission bounds retained archives; the worker separately bounds
            # schema CPU time and can be terminated when this request is cancelled.
            async with self.validator.admission():
                try:
                    async with asyncio.timeout(
                        self.charts.source.http.settings.repository_timeout_seconds
                    ):
                        body = await self.charts.download_archive(draft.chart)
                except TimeoutError:
                    # Download deadlines are upstream failures, not invalid choices.
                    raise SourceError(504, "source_timeout") from None
                result = await self.validator.configuration(
                    body,
                    draft,
                    overrides,
                    confidential,
                    {item.target: item.name for item in kubernetes_secrets},
                )
        finally:
            confidential.clear()
        # Validation-only must also reject authorization/eligibility changes during I/O.
        async with self.database.session() as session:
            installation = await self._installation(session, principal, installation_id)
            await self._release(session, principal, release_id)
            if await self.charts.lock_pin(session, draft.chart_version_id) != draft.chart:
                raise DefinitionError(409)
            await self._bindings(
                session, installation, secrets, kubernetes_secrets, load_payloads=False
            )
        return draft, result

    async def append(
        self,
        principal: Principal,
        installation_id: UUID,
        release_id: UUID,
        overrides: JsonRecord,
        secrets: list[SecretBinding],
        kubernetes_secrets: list[KubernetesSecretBinding],
    ) -> InstallationRevision:
        """Validate without locks, then atomically append bindings and audited history.

        Each valid request adds an independent history entry. The short installation
        lock keeps application lineage and bindings consistent. Cancellation/failure
        rolls back the complete write.
        """
        draft, result = await self.prepare(
            principal, installation_id, release_id, overrides, secrets, kubernetes_secrets
        )
        if result.issues:
            raise PublicationFailure(422, "invalid_installation_configuration", result.issues)
        try:
            async with self.database.transaction() as session:
                # Lock order matches other writers: installation, release/app,
                # repository, then secret versions ordered by ID. External work
                # is finished before taking any of these commit-phase locks.
                installation = await self._installation(
                    session, principal, installation_id, lock=True
                )
                release = await self._release(session, principal, release_id)
                if await self.charts.lock_pin(session, release.chart_version_id) != draft.chart:
                    raise DefinitionError(409)
                # The installation lock serializes the first application's choice.
                # Read only that identity, not a full historical configuration.
                application_id = (
                    await session.exec(
                        select(Release.application_id)
                        .join(
                            InstallationRevision,
                            col(InstallationRevision.release_id) == col(Release.id),
                        )
                        .where(col(InstallationRevision.installation_id) == installation_id)
                        .order_by(
                            col(InstallationRevision.created).desc(),
                            col(InstallationRevision.id).desc(),
                        )
                        .limit(1)
                    )
                ).first()
                if application_id is not None and application_id != release.application_id:
                    raise DefinitionError(409)
                await self._bindings(
                    session, installation, secrets, kubernetes_secrets, load_payloads=False
                )
                record = InstallationRevision(
                    installation_id=installation_id,
                    release_id=release_id,
                    overrides=copy.deepcopy(overrides),
                    values=result.values,
                )
                session.add(record)
                await session.flush()
                for binding in secrets:
                    session.add(RevisionSecret(revision_id=record.id, **binding.model_dump()))
                for reference in kubernetes_secrets:
                    session.add(
                        RevisionKubernetesSecret(
                            revision_id=record.id, **reference.model_dump(exclude={"namespace"})
                        )
                    )
                await session.flush()
            return record
        except IntegrityError:
            # Translate FK/constraint races at this service-owned write boundary;
            # the transaction context has already rolled back rows and audit.
            raise DefinitionError(409) from None
