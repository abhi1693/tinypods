"""Authorized async persistence for shared application definitions."""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import func, inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.applications.models import Application, ApplicationCategory, Category, Release
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.auth import Principal, Scope
from tinypods_core.auth.authorization import AuthorizationService

DEFINITION_FIELDS: dict[type[AuditTrailMixin], frozenset[str]] = {
    Application: frozenset({"name", "slug", "description", "icon", "status"}),
    Category: frozenset({"name", "slug", "description", "icon"}),
    Release: frozenset(
        {
            "application_id",
            "chart_version",
            "source_type",
            "source_location",
            "chart_name",
            "digest",
            "configuration_fields",
        }
    ),
}


def validate_page(limit: int, offset: int) -> None:
    """Bound service-level collection reads independently of HTTP query validation."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Invalid definition page")


class DefinitionError(Exception):
    """Carry only a deliberate public status, without database details."""

    def __init__(self, status: int) -> None:
        """Retain the public not-found/conflict classification."""
        self.status = status
        super().__init__(status)


class DefinitionService[Record: AuditTrailMixin]:
    """Own scoped queries and transactions for the three shared definition entities.

    Authentication is revalidated before reads; only installation owners can
    mutate these shared definitions. This is not an installed-app service.
    """

    def __init__(
        self, model: type[Record], session: AsyncSession, authorization: AuthorizationService
    ) -> None:
        """Retain a request-owned session and a trusted, code-selected model."""
        self.model = model
        self.session = session
        self.authorization = authorization
        mapper = inspect(model)
        assert mapper is not None
        self.columns = mapper.columns
        self.writable = DEFINITION_FIELDS[model]

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        offset: int,
        ordering: str,
        q: str | None = None,
        filters: Mapping[str, str | UUID] | None = None,
    ) -> tuple[list[Record], int]:
        """Return one bounded, deterministically ordered page after identity validation.

        Args:
            principal: Current credential-backed identity to revalidate.
            limit: Page size, already bounded by the public query DTO.
            offset: Nonnegative page offset.
            ordering: Explicitly allowlisted column, optionally prefixed by a minus.
            q: Optional case-insensitive literal substring of name or chart name.
            filters: Exact filters selected by typed resource query DTOs.

        Returns:
            Page records and the matching total before pagination.
        """
        await self.authorization.validator.validate_principal(principal)
        allowed_order = (
            {"created", "name"} if self.model is not Release else {"created", "chart_version"}
        )
        key = ordering.removeprefix("-")
        validate_page(limit, offset)
        if key not in allowed_order:
            raise ValueError("Invalid definition page or ordering")
        query = select(self.model)
        if q:
            search = self.columns["chart_name" if self.model is Release else "name"]
            query = query.where(search.icontains(q, autoescape=True))
        allowed_filters = (
            {"status", "slug"}
            if self.model is Application
            else {"slug"}
            if self.model is Category
            else {"application_id", "source_type"}
        )
        for name, value in (filters or {}).items():
            if name not in allowed_filters:
                raise ValueError("Unknown definition filter")
            query = query.where(self.columns[name] == value)
        total = (await self.session.exec(select(func.count()).select_from(query.subquery()))).one()
        order = self.columns[key]
        query = (
            query.order_by(
                order.desc() if ordering.startswith("-") else order.asc(), self.columns["id"]
            )
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.exec(query)).all()), total

    async def get(self, principal: Principal, identity: UUID) -> Record:
        """Read one definition only after revalidating the supplied credential."""
        await self.authorization.validator.validate_principal(principal)
        return await self._get(identity)

    async def _get(self, identity: UUID, *, lock: bool = False) -> Record:
        """Resolve one definition and optionally lock it inside the write transaction."""
        query = select(self.model).where(self.columns["id"] == identity)
        if lock:
            query = query.with_for_update()
        record = (await self.session.exec(query)).one_or_none()
        if record is None:
            raise DefinitionError(404)
        return record

    async def save(
        self, principal: Principal, values: Mapping[str, object], *, identity: UUID | None = None
    ) -> Record:
        """Create or update a definition and its automatic audit in one transaction.

        Args:
            principal: Revalidated installation-owner identity.
            values: Explicit public write DTO fields; never a persistence-model dump.
            identity: Existing object to lock and modify, or None to create.

        Returns:
            Committed definition with stable identity and timestamps.

        Raises:
            DefinitionError: A referenced record is absent or a constraint conflicts.
        """
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        if set(values) - self.writable:
            raise ValueError("Unknown definition write field")
        try:
            async with self.session.begin():
                record = (
                    self.model.model_validate(values)
                    if identity is None
                    else await self._get(identity, lock=True)
                )
                for name, value in values.items():
                    setattr(record, name, value)
                self.session.add(record)
                await self.session.flush()
            return record
        except IntegrityError as exc:
            raise DefinitionError(409) from exc

    async def delete(self, principal: Principal, identity: UUID) -> None:
        """Delete an unreferenced definition atomically, preserving audit history."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        try:
            async with self.session.begin():
                await self.session.delete(await self._get(identity, lock=True))
        except IntegrityError as exc:
            raise DefinitionError(409) from exc


class MembershipService:
    """Own category membership operations without inventing a separate identity."""

    def __init__(self, session: AsyncSession, authorization: AuthorizationService) -> None:
        """Retain request-owned persistence and credential-neutral authorization."""
        self.session = session
        self.authorization = authorization

    async def list(
        self, principal: Principal, application_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[ApplicationCategory], int]:
        """List a bounded page of memberships for an existing application."""
        await self.authorization.validator.validate_principal(principal)
        validate_page(limit, offset)
        if await self.session.get(Application, application_id) is None:
            raise DefinitionError(404)
        query = select(ApplicationCategory).where(
            ApplicationCategory.application_id == application_id
        )
        total = (await self.session.exec(select(func.count()).select_from(query.subquery()))).one()
        rows = await self.session.exec(
            query.order_by(col(ApplicationCategory.category_id)).limit(limit).offset(offset)
        )
        return list(rows.all()), total

    async def add(
        self, principal: Principal, application_id: UUID, category_id: UUID
    ) -> ApplicationCategory:
        """Add one protected pair atomically; reject absent targets and duplicates."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        try:
            async with self.session.begin():
                if (
                    await self.session.get(Application, application_id) is None
                    or await self.session.get(Category, category_id) is None
                ):
                    raise DefinitionError(404)
                record = ApplicationCategory(application_id=application_id, category_id=category_id)
                self.session.add(record)
                await self.session.flush()
            return record
        except IntegrityError as exc:
            raise DefinitionError(409) from exc

    async def delete(self, principal: Principal, application_id: UUID, category_id: UUID) -> None:
        """Remove the exact pair without deleting either referenced entity."""
        await self.authorization.require_access(principal, Scope.CATALOG_ADMIN)
        async with self.session.begin():
            record = await self.session.get(ApplicationCategory, (application_id, category_id))
            if record is None:
                raise DefinitionError(404)
            await self.session.delete(record)
