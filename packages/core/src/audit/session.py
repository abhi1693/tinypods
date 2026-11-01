"""Unit-of-work session blocking legacy bulk APIs that omit flush events."""

# ruff: noqa: ANN401 - SQLAlchemy legacy override signatures

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Mapper
from sqlmodel import Session


class ChangeSession(Session):
    """Runtime session: audited writes must use add/delete and normal flush."""

    def bulk_save_objects(
        self,
        objects: Iterable[object],
        return_defaults: bool = False,
        update_changed_only: bool = True,
        preserve_order: bool = True,
    ) -> None:
        """Reject legacy bulk persistence, which bypasses transactional capture."""
        raise ValueError("Legacy bulk persistence bypasses object change logging")

    def bulk_insert_mappings(
        self,
        mapper: Mapper[Any] | type[Any],
        mappings: Iterable[Mapping[str, Any]],
        return_defaults: bool = False,
        render_nulls: bool = False,
    ) -> None:
        """Reject legacy bulk inserts; use add_all with a bounded transaction."""
        raise ValueError("Legacy bulk persistence bypasses object change logging")

    def bulk_update_mappings(
        self, mapper: Mapper[Any] | type[Any], mappings: Iterable[Mapping[str, Any]]
    ) -> None:
        """Reject legacy bulk updates; load and mutate registered instances."""
        raise ValueError("Legacy bulk persistence bypasses object change logging")
