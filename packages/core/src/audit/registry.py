"""Explicit model registration and controlled idempotent type synchronization."""

from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Connection, inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm.base import NO_VALUE
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.json_types import JsonRecord, JsonValue


def safe_value(value: object) -> JsonValue:
    """Encode bounded approved scalar/list values; reject arbitrary object traversal.

    Args:
        value: Explicitly allowlisted attribute value.

    Returns:
        JSON-compatible detached data.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (UUID, datetime)):
        return str(value)
    raise ValueError("Change-log fields must be approved scalar values")


@dataclass(frozen=True)
class ModelRegistration:
    """An installed model's safe scalar fields and declared relationship identities."""

    model: type[SQLModel]
    app_label: str
    name: str
    fields: tuple[str, ...]
    identity: str = "id"
    relationships: tuple[str, ...] = ()
    related: tuple[str, type[SQLModel]] | None = None

    @cached_property
    def type_id(self) -> UUID:
        """Keep deterministic identities across repeated migrations and process starts."""
        return uuid5(NAMESPACE_URL, f"tinypods:{self.app_label}.{self.name}")

    def serialize(self, obj: SQLModel) -> JsonRecord:
        """Copy only registered fields; relationships must be explicitly loaded.

        Args:
            obj: Registered mapped object owned by the caller's session.

        Returns:
            Detached safe snapshot with complete declared relationship identities.
        """
        state = inspect(obj)
        assert state is not None
        result: JsonRecord = {field: safe_value(getattr(obj, field)) for field in self.fields}
        for name in self.relationships:
            value = state.attrs[name].loaded_value
            if value is NO_VALUE:
                raise ValueError("Load declared audit relationships before mutation")
            members = value if isinstance(value, list) else ([] if value is None else [value])
            result[name] = list[JsonValue](
                sorted(str(getattr(member, registry[type(member)].identity)) for member in members)
            )
        return result


registry: dict[type[SQLModel], ModelRegistration] = {}


def register(spec: ModelRegistration) -> None:
    """Register trusted code explicitly; never import module paths from database data.

    Args:
        spec: Installed model and reviewed allowlists.
    """
    if spec.model in registry:
        raise ValueError("Model is already registered for change logging")
    if any(item.type_id == spec.type_id for item in registry.values()):
        raise ValueError("Object type label is already registered")
    mapper = inspect(spec.model)
    assert mapper is not None
    for field in (*spec.fields, spec.identity):
        if field not in mapper.columns:
            raise ValueError("Unknown change-log scalar field")
    for name in spec.relationships:
        if name not in mapper.relationships:
            raise ValueError("Unknown change-log relationship")
    registry[spec.model] = spec


def sync_object_types(connection: Connection) -> None:
    """Insert explicitly registered types after migrations, retaining historical types.

    Args:
        connection: Migration-owned connection in the caller's transaction.
    """
    from tinypods_core.audit.models import ObjectType

    for spec in registry.values():
        connection.execute(
            insert(ObjectType)
            .values(id=spec.type_id, app_label=spec.app_label, model=spec.name)
            .on_conflict_do_nothing(index_elements=["app_label", "model"])
        )


@dataclass(frozen=True)
class ObjectReference:
    """Generic reference resolved only through the current installed registry."""

    type_id: UUID
    object_id: str

    async def resolve(self, session: AsyncSession) -> SQLModel | None:
        """Return a surviving installed target, or None for removed types/objects.

        Args:
            session: Caller-owned async session; resolution grants no authorization.

        Returns:
            Registered model instance when the target still exists.
        """
        spec = next((item for item in registry.values() if item.type_id == self.type_id), None)
        if spec is None:
            return None
        mapper = inspect(spec.model)
        assert mapper is not None
        column = mapper.columns[spec.identity]
        try:
            value = column.type.python_type(self.object_id)
        except (ValueError, TypeError):
            return None
        result = await session.exec(select(spec.model).where(column == value))
        return result.one_or_none()
