"""Authorized draft cloning, validation and atomic immutable release publication."""

import asyncio
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from tinypods_core import __version__
from tinypods_core.applications.contracts import ReleaseValidationIssue
from tinypods_core.applications.definitions import ReleaseState
from tinypods_core.applications.models import Application, Release
from tinypods_core.applications.service import DefinitionError
from tinypods_core.applications.validation import (
    PublicationValidator,
    ReleaseDraft,
    ValidatedDefinition,
)
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.db import Database
from tinypods_core.json_types import JsonRecord
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.verification import ChartVerification


class PublicationFailure(Exception):
    """A deliberate public failure containing safe issue codes, never raw chart values."""

    def __init__(
        self, status: int, code: str, issues: tuple[ReleaseValidationIssue, ...] = ()
    ) -> None:
        """Retain the operation classification and optional validation failures."""
        self.status, self.code, self.issues = status, code, issues
        super().__init__(code)


@dataclass(frozen=True)
class DraftState:
    """Detached draft payload and optimistic generation captured before external I/O."""

    definition: ReleaseDraft
    generation: datetime


class ReleasePublicationService:
    """Coordinate verification outside short, locked release-write transactions."""

    def __init__(
        self,
        database: Database,
        authorization: AuthorizationService,
        charts: ChartVerification,
        validator: PublicationValidator,
    ) -> None:
        """Borrow application-owned authorization, artifact verification and validation services."""
        self.database, self.authorization = database, authorization
        self.charts, self.validator = charts, validator

    async def draft(self, identity: UUID) -> DraftState:
        """Read a detached draft without holding a connection during archive retrieval."""
        async with self.database.session() as session:
            release = await session.get(Release, identity)
            if release is None:
                raise DefinitionError(404)
            if release.state == "published" or release.revoked_at is not None:
                raise DefinitionError(409)
            options: JsonRecord = {
                "architectures": [str(item) for item in release.architectures],
                "renderer": release.renderer,
                "renderer_contract": release.renderer_contract,
                "core_version_min": release.core_version_min,
                "core_version_max": release.core_version_max,
            }
            application_id, version_id, revision, generation = (
                release.application_id,
                release.chart_version_id,
                release.revision,
                release.last_updated,
            )
            fields = copy.deepcopy(release.configuration_fields)
        chart = await self.charts.pin(version_id)
        return DraftState(
            ReleaseDraft(
                identity, application_id, version_id, revision, fields, options, __version__, chart
            ),
            generation,
        )

    async def checked(self, principal: Principal, state: DraftState) -> ValidatedDefinition:
        """Verify exact bytes and CPU-bound schemas, then revalidate authorization and source."""
        definition = state.definition
        async with self.validator.admission():
            try:
                async with asyncio.timeout(
                    self.charts.source.http.settings.repository_timeout_seconds
                ):
                    body = await self.charts.download_archive(definition.chart)
            except TimeoutError:
                raise SourceError(504, "source_timeout") from None
            result = await self.validator.validate_admitted(body, definition)
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        if await self.charts.pin(definition.chart_version_id) != definition.chart:
            raise DefinitionError(409)
        return result

    async def validate(self, principal: Principal, identity: UUID) -> ValidatedDefinition:
        """Validate draft or review-state deployment fields without changing visibility."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        before = await self.draft(identity)
        result = await self.checked(principal, before)
        if await self.draft(identity) != before:
            raise DefinitionError(409)
        return result

    async def publish(self, principal: Principal, identity: UUID) -> UUID:
        """Validate the existing definition, then publish and audit one atomic state transition."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        async with self.database.session() as session:
            current = await session.get(Release, identity)
            if current is None:
                raise DefinitionError(404)
            if current.revoked_at is not None:
                raise DefinitionError(409)
            if current.state == "published":
                return identity
        before = await self.draft(identity)
        validated = await self.checked(principal, before)
        if validated.issues:
            raise PublicationFailure(422, "invalid_release_definition", validated.issues)
        try:
            async with self.database.transaction() as session:
                query = select(Release).where(Release.id == identity).with_for_update()
                rows = await session.exec(query)
                release = rows.one_or_none()
                if release is None:
                    raise DefinitionError(404)
                if release.revoked_at is not None:
                    raise DefinitionError(409)
                if release.state != "published":
                    if release.last_updated != before.generation:
                        raise DefinitionError(409)
                    # Lock source parents before children, matching repository sync/delete.
                    # Keep these locks until the state/audit commit, never during network I/O.
                    if (
                        await self.charts.lock_pin(session, release.chart_version_id)
                        != before.definition.chart
                    ):
                        raise DefinitionError(409)
                    release.state = ReleaseState.PUBLISHED
                    release.published_at = release.published_at or datetime.now(UTC)
                    session.add(release)
                    await session.flush()
        except IntegrityError:
            raise DefinitionError(409) from None
        return identity

    async def clone(self, principal: Principal, identity: UUID) -> UUID:
        """Create a fresh draft revision without changing an existing release or its pins."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        async with self.database.transaction() as session:
            source_query = select(Release).where(Release.id == identity).with_for_update()
            source_result = await session.exec(source_query)
            source = source_result.one_or_none()
            if source is None:
                raise DefinitionError(404)
            # Lock order matches existing release writes: release first, app second.
            # The app lock serializes revision assignment across different source clones.
            application_query = select(Application)
            application_query = application_query.where(
                Application.id == source.application_id
            ).with_for_update()
            await session.exec(application_query)
            revision_query = select(func.max(Release.revision)).where(
                Release.application_id == source.application_id,
                Release.chart_version_id == source.chart_version_id,
            )
            revision_result = await session.exec(revision_query)
            maximum = revision_result.one()
            clone = Release(
                application_id=source.application_id,
                chart_version_id=source.chart_version_id,
                revision=(maximum or 0) + 1,
                configuration_fields=copy.deepcopy(source.configuration_fields),
                architectures=list(source.architectures),
                renderer=source.renderer,
                renderer_contract=source.renderer_contract,
                core_version_min=source.core_version_min,
                core_version_max=source.core_version_max,
                notes=source.notes,
            )
            session.add(clone)
            await session.flush()
            result = clone.id
        return result

    async def lifecycle(
        self,
        principal: Principal,
        identity: UUID,
        reason: str,
        *,
        deprecated: bool | None = None,
        revoke: bool = False,
    ) -> None:
        """Audit metadata-only lifecycle changes while keeping all deployment inputs frozen."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        if not reason.strip() or len(reason) > 1000 or (deprecated is None and not revoke):
            raise DefinitionError(422)
        async with self.database.transaction() as session:
            release = (
                await session.exec(select(Release).where(Release.id == identity).with_for_update())
            ).one_or_none()
            if release is None:
                raise DefinitionError(404)
            if release.published_at is None:
                raise DefinitionError(409)
            if deprecated is not None:
                release.deprecated = deprecated
            if revoke and release.revoked_at is None:
                release.revoked_at = datetime.now(UTC)
            release.lifecycle_reason = reason
            session.add(release)
