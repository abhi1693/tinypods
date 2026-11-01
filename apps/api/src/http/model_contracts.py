"""Shared identity/audit DTOs and partial-update null semantics."""

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, model_validator
from tinypods_api.http.contracts import RequestDTO
from tinypods_api.http.representations import ResourceRead


class DefinitionRead(ResourceRead):
    """Safe identity and audit timestamps shared by definition responses."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created: datetime
    last_updated: datetime


class DefinitionPatch(RequestDTO):
    """Reject explicit nulls except for a nullable icon; preserve omitted fields."""

    @model_validator(mode="after")
    def validate_nulls(self) -> "DefinitionPatch":
        """Distinguish absence from clearing nullable data on partial updates."""
        if any(getattr(self, name) is None for name in self.model_fields_set - {"icon"}):
            raise ValueError("Only icon may be explicitly null")
        return self
