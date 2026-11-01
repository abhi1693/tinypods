"""Public representation metadata and model-declared related-object schemas."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import copy
from functools import cache
from typing import Any, ClassVar, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_core.persistence import CoreModel
from typing_extensions import TypeForm

Identity = tuple[UUID, ...]
ComputedValues = Mapping[Identity, Mapping[str, object]]
ComputedResolver = Callable[
    [AsyncSession, Sequence[CoreModel], frozenset[str]], Awaitable[ComputedValues]
]


class ResourceRead(BaseModel):
    """Tag a public DTO with its persistence owner and optional computed-value provider."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")
    resource_model: ClassVar[type[CoreModel] | None] = None
    computed: ClassVar[ComputedResolver | None] = None
    is_brief: ClassVar[bool] = False

    @classmethod
    def __pydantic_on_complete__(cls) -> None:
        """Reject brief declarations outside the completed public representation."""
        model = cls.resource_model
        if model is not None and not cls.is_brief and model.brief_fields:
            fields = model.brief_fields
            if not isinstance(fields, tuple) or len(set(fields)) != len(fields):
                raise ValueError("brief_fields must be a tuple of distinct public field names")
            if set(fields) - cls.model_fields.keys():
                raise ValueError("brief_fields must be part of the public representation")


class Related[DTO: ResourceRead](ResourceRead):
    """Select a related DTO's model-declared brief fields, or its full public DTO.

    This schema factory carries no database access. Its generated classes inherit
    ResourceRead, so the same selected fields drive prefetching and OpenAPI output.
    """

    is_brief: ClassVar[bool] = True

    @classmethod
    def __class_getitem__(
        cls, typevar_values: TypeForm[Any] | tuple[TypeForm[Any], ...]
    ) -> type[BaseModel]:
        """Resolve one public DTO into its cached related-object representation."""
        if not isinstance(typevar_values, type) or not issubclass(typevar_values, ResourceRead):
            raise TypeError("Related requires one public resource DTO")
        return related_schema(typevar_values)


@cache
def related_schema(dto: type[ResourceRead]) -> type[ResourceRead]:
    """Build a read-only schema from a public DTO and its model's brief declaration.

    Args:
        dto: Full public representation, including its explicitly registered model.

    Returns:
        A cached brief DTO, or the full public DTO when the declaration is absent
        or empty. No fallback traverses private persistence fields.
    """
    model = dto.resource_model
    if model is None:
        raise ValueError("Related resources require a registered public model")
    if not model.brief_fields:
        return dto
    definitions: dict[str, tuple[TypeForm[Any], FieldInfo]] = {}
    for name in model.brief_fields:
        source = dto.model_fields.get(name)
        # During a self-referencing DTO's construction only inherited fields are
        # complete. Its scalar brief fields can be read from the model; the DTO's
        # completion hook then checks that every selected field is public.
        if source is None and not dto.__pydantic_complete__:
            source = model.model_fields.get(name)
        if source is None:
            raise ValueError("Unknown brief field in public representation")
        field = copy(source)
        field.default = PydanticUndefined
        field.default_factory = None
        definitions[name] = (cast(TypeForm[Any], field.annotation), field)
    # Dynamic field names cross Pydantic's keyword-based factory boundary here.
    schema = cast(
        type[ResourceRead],
        create_model(
            dto.__name__.removesuffix("Read") + "Brief",
            __base__=cast(type[ResourceRead], Related),
            **cast(dict[str, Any], definitions),
        ),
    )
    schema.resource_model = model
    schema.computed = dto.computed
    return schema
