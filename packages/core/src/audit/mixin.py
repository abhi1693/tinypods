"""Abstract timestamp and explicit snapshot behavior for audited models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import PrivateAttr
from sqlalchemy import DateTime
from sqlmodel import Field
from tinypods_core.json_types import JsonRecord
from tinypods_core.persistence import CoreModel

if TYPE_CHECKING:
    from tinypods_core.audit.models import AuditRecord

type AuditAction = Literal["create", "update", "delete"]


class AuditTrailMixin(CoreModel):
    """Abstract safe snapshot interface; concrete models need explicit registration."""

    created: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    last_updated: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    _prechange: JsonRecord | None = PrivateAttr(default=None)

    def serialize_object(self) -> JsonRecord:
        """Return only registered safe fields and explicitly loaded relationships."""
        from tinypods_core.audit.registry import registry

        return registry[type(self)].serialize(self)

    def snapshot(self) -> None:
        """Remember a detached, allowlisted prechange snapshot for explicit conversion."""
        self._prechange = self.serialize_object()

    def to_audit_record(self, action: AuditAction) -> AuditRecord:
        """Build an unsaved history record; automatic capture owns normal persistence.

        Args:
            action: Lifecycle action; update/delete use the most recent snapshot.

        Returns:
            A detached change record without any credential material.
        """
        from tinypods_core.audit.capture import make_record
        from tinypods_core.audit.registry import registry

        spec = registry[type(self)]
        return make_record(
            spec,
            self,
            action,
            None if action == "create" else (self._prechange or self.serialize_object()),
            None if action == "delete" else self.serialize_object(),
        )
