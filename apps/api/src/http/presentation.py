"""Reusable eager loading and pure public response projection."""

from collections.abc import Mapping, Sequence
from functools import cache
from types import UnionType
from typing import Union, cast, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import inspect, tuple_
from sqlalchemy.orm import Load
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_api.http.representations import (
    ComputedResolver,
    Identity,
    ResourceRead,
)
from tinypods_core.applications.service import DefinitionError, DefinitionService, MembershipService
from tinypods_core.audit import AuditTrailMixin
from tinypods_core.auth import Principal
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.persistence import CoreModel


def relation_type(annotation: object) -> tuple[type[ResourceRead], bool] | None:
    """Identify public related DTOs inside optional and collection annotations."""
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        candidates = [value for value in get_args(annotation) if value is not type(None)]
        return relation_type(candidates[0]) if len(candidates) == 1 else None
    if origin is list:
        result = relation_type(get_args(annotation)[0])
        return (result[0], True) if result is not None else None
    if isinstance(annotation, type) and issubclass(annotation, ResourceRead):
        if annotation.resource_model is None:
            raise ValueError("Related DTO has no public resource model")
        return annotation, False
    return None


def identity(record: CoreModel) -> Identity:
    """Read stable UUID primary keys without accessing unloaded attributes."""
    state = inspect(record)
    assert state is not None
    values = state.identity
    if values is None or not all(isinstance(value, UUID) for value in values):
        raise ValueError("Public records require persisted UUID primary keys")
    return cast(Identity, values)


def loaded(record: CoreModel, name: str) -> object:
    """Fail before an accidental lazy query instead of hiding I/O in serialization."""
    state = inspect(record)
    assert state is not None
    if name in state.unloaded:
        raise ValueError("Public representation was not prefetched")
    return getattr(record, name)


@cache
def loading_options(
    schema: type[ResourceRead], ancestors: tuple[type[ResourceRead], ...] = ()
) -> tuple[Load, ...]:
    """Compile an eager-load graph using exactly the public schema's selected fields.

    Single relations are joined; collections use select-in batches. Scalars and
    relationships outside the graph raise on access, preventing serializer N+1 I/O.
    """
    model = schema.resource_model
    if model is None or schema in ancestors:
        raise ValueError("Public relationship graph requires a finite registered representation")
    mapper = inspect(model)
    assert mapper is not None
    columns = {column.key for column in mapper.primary_key}
    options: list[Load] = []
    for name, field in schema.model_fields.items():
        related = relation_type(field.annotation)
        if related is None:
            if name not in mapper.column_attrs:
                raise ValueError("Public scalar field must be mapped")
            columns.add(name)
            continue
        target, many = related
        relationship = mapper.relationships.get(name)
        if relationship is None or relationship.mapper.class_ is not target.resource_model:
            raise ValueError("Public relationship does not match its mapped target")
        if bool(relationship.uselist) != many:
            raise ValueError("Public relationship cardinality does not match its schema")
        columns.update(column.key for column in relationship.local_columns)
        attribute = getattr(model, name)
        loader = Load(model).selectinload(attribute) if many else Load(model).joinedload(attribute)
        options.append(loader.options(*loading_options(target, ancestors + (schema,))))
    options.append(
        Load(model)
        .load_only(*(getattr(model, name) for name in sorted(columns)), raiseload=True)
        .raiseload("*")
    )
    return tuple(options)


type Overrides = dict[tuple[type[CoreModel], Identity], Mapping[str, object]]
type Computations = dict[
    tuple[type[CoreModel], ComputedResolver], tuple[dict[Identity, CoreModel], set[str]]
]


def collect(schema: type[ResourceRead], record: CoreModel, computations: Computations) -> None:
    """Collect computed-field work from an already eager-loaded graph without SQL."""
    model = schema.resource_model
    assert model is not None
    if schema.computed is not None:
        records, fields = computations.setdefault((model, schema.computed), ({}, set()))
        records[identity(record)] = record
        fields.update(schema.model_fields)
    for name, field in schema.model_fields.items():
        related = relation_type(field.annotation)
        if related is None:
            continue
        target, many = related
        value = loaded(record, name)
        if value is None:
            continue
        members = cast(Sequence[CoreModel], value) if many else [cast(CoreModel, value)]
        for member in members:
            collect(target, member, computations)


def serialize(
    schema: type[ResourceRead], record: CoreModel, overrides: Overrides
) -> dict[str, object]:
    """Build detached public values using only selected, already loaded attributes."""
    model = schema.resource_model
    assert model is not None
    computed = overrides.get((model, identity(record)), {})
    values: dict[str, object] = {}
    for name, field in schema.model_fields.items():
        if name in computed:
            values[name] = computed[name]
            continue
        value = loaded(record, name)
        related = relation_type(field.annotation)
        if related is None or value is None:
            values[name] = value
            continue
        target, many = related
        values[name] = (
            [serialize(target, member, overrides) for member in cast(Sequence[CoreModel], value)]
            if many
            else serialize(target, cast(CoreModel, value), overrides)
        )
    return values


class Presenter:
    """Own authorized loading; serialization runs only after this stage completes."""

    def __init__(self, session: AsyncSession, authorization: AuthorizationService) -> None:
        """Retain a request-owned session and the existing resource access boundary."""
        self.session = session
        self.authorization = authorization

    async def _load(
        self, schema: type[ResourceRead], identities: Sequence[Identity]
    ) -> dict[Identity, CoreModel]:
        """Load a bounded identity batch with its declared nested fields."""
        model = schema.resource_model
        assert model is not None
        mapper = inspect(model)
        assert mapper is not None
        query = (
            select(model)
            .where(tuple_(*mapper.primary_key).in_(identities))
            .options(*loading_options(schema))
            .execution_options(populate_existing=True)
        )
        records = (await self.session.exec(query)).unique().all()
        return {identity(record): record for record in records}

    async def _computed(self, computations: Computations) -> Overrides:
        """Run each computed provider once for its distinct records across the page."""
        results: Overrides = {}
        for (model, resolver), (records, fields) in computations.items():
            values = await resolver(self.session, list(records.values()), frozenset(fields))
            results.update({(model, key): value for key, value in values.items()})
        return results

    async def many[DTO: ResourceRead](
        self, principal: Principal, schema: type[DTO], records: Sequence[CoreModel]
    ) -> list[DTO]:
        """Prefetch a whole page and return typed, detached public representations.

        Args:
            principal: Current credential-backed identity, revalidated before reads.
            schema: Full public response schema; nested fields select their own brief DTOs.
            records: Already authorized records in the original deterministic page order.

        Returns:
            DTOs in input order. Query count depends on the relation graph, not page size.
        """
        await self.authorization.validator.validate_principal(principal)
        if not records:
            return []
        keys = [identity(record) for record in records]
        prefetched = await self._load(schema, keys)
        if set(keys) - prefetched.keys():
            raise DefinitionError(404)
        computations: Computations = {}
        for key in keys:
            collect(schema, prefetched[key], computations)
        overrides = await self._computed(computations)
        return [
            schema.model_validate(serialize(schema, prefetched[key], overrides)) for key in keys
        ]

    async def values[DTO: BaseModel](
        self, principal: Principal, schema: type[DTO], source: Mapping[str, object]
    ) -> DTO:
        """Project action results containing scalar foreign keys through the same loader."""
        await self.authorization.validator.validate_principal(principal)
        targets: dict[type[ResourceRead], set[Identity]] = {}
        references: dict[str, tuple[type[ResourceRead], Identity] | None] = {}
        for name, field in schema.model_fields.items():
            related = relation_type(field.annotation)
            if related is None:
                continue
            target, many = related
            if many:
                raise ValueError("Action projections require single related references")
            value = source.get(name + "_id")
            if value is None:
                references[name] = None
                continue
            if not isinstance(value, UUID):
                raise ValueError("Action relation identities must be UUIDs")
            key: Identity = (value,)
            references[name] = (target, key)
            targets.setdefault(target, set()).add(key)
        records: dict[type[ResourceRead], dict[Identity, CoreModel]] = {}
        computations: Computations = {}
        for target, keys in targets.items():
            batch = await self._load(target, list(keys))
            if keys - batch.keys():
                raise DefinitionError(404)
            records[target] = batch
            for record in batch.values():
                collect(target, record, computations)
        overrides = await self._computed(computations)
        values: dict[str, object] = {}
        for name in schema.model_fields:
            if name not in references:
                values[name] = source[name]
            elif references[name] is None:
                values[name] = None
            else:
                target, key = cast(tuple[type[ResourceRead], Identity], references[name])
                values[name] = serialize(target, records[target][key], overrides)
        return schema.model_validate(values)


class ResourceService[Record: AuditTrailMixin, DTO: ResourceRead]:
    """Adapt existing CRUD services to one reusable prefetched response pipeline."""

    def __init__(self, core: DefinitionService[Record], schema: type[DTO]) -> None:
        """Keep writes in their domain service and presentation in the API layer."""
        self.core = core
        self.schema = schema
        self.presenter = Presenter(core.session, core.authorization)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        offset: int,
        ordering: str,
        q: str | None = None,
        filters: Mapping[str, str | UUID | None] | None = None,
    ) -> tuple[list[DTO], int]:
        """Load a typed, eager-prefetched page after the domain's scoped query."""
        records, total = await self.core.list(
            principal, limit=limit, offset=offset, ordering=ordering, q=q, filters=filters
        )
        return await self.presenter.many(principal, self.schema, records), total

    async def get(self, principal: Principal, identity: UUID) -> DTO:
        """Read and project one authorized record."""
        record = await self.core.get(principal, identity)
        return (await self.presenter.many(principal, self.schema, [record]))[0]

    async def save(
        self, principal: Principal, values: Mapping[str, object], *, identity: UUID | None = None
    ) -> DTO:
        """Commit through the existing writer, then project its standard read response."""
        record = await self.core.save(principal, values, identity=identity)
        return (await self.presenter.many(principal, self.schema, [record]))[0]

    async def delete(self, principal: Principal, identity: UUID) -> None:
        """Delegate protected deletion and audit behavior to the domain service."""
        await self.core.delete(principal, identity)


class MembershipResource[DTO: ResourceRead]:
    """Project composite-key memberships through the shared presenter."""

    def __init__(self, core: MembershipService, schema: type[DTO]) -> None:
        """Retain membership writes and the same page-level loading pipeline."""
        self.core = core
        self.schema = schema
        self.presenter = Presenter(core.session, core.authorization)

    async def list(
        self, principal: Principal, application_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[DTO], int]:
        """Return typed memberships without per-membership relation queries."""
        rows, total = await self.core.list(principal, application_id, limit=limit, offset=offset)
        return await self.presenter.many(principal, self.schema, rows), total

    async def add(self, principal: Principal, application_id: UUID, category_id: UUID) -> DTO:
        """Create an exact pair and return its prefetched public references."""
        record = await self.core.add(principal, application_id, category_id)
        return (await self.presenter.many(principal, self.schema, [record]))[0]

    async def delete(self, principal: Principal, application_id: UUID, category_id: UUID) -> None:
        """Remove the membership through its existing scoped domain operation."""
        await self.core.delete(principal, application_id, category_id)
