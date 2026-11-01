"""Indexed region inheritance with database-maintained ancestry."""

from uuid import UUID

from sqlalchemy import func, true
from sqlalchemy.orm import aliased
from sqlmodel import col, select
from sqlmodel.sql.expression import Select
from tinypods_core.applications.service import DefinitionService
from tinypods_core.clusters.models import Region


def flag_query(identities: list[UUID]) -> Select[UUID, str]:
    """Resolve nearest visual overrides for one bounded, authorized page.

    Args:
        identities: Region IDs already selected by the authenticated resource query.

    Returns:
        An ID and flag for each region with a visual override on its ancestor path.
        Regions without an inherited flag are absent from this result.
    """
    ancestor = aliased(Region)
    # Paths contain stable UUIDs in root-to-node order. Containment identifies
    # ancestors, and the longest matching path provides the nearest override.
    # A partial ID index considers only rows that actually define a flag.
    nearest = (
        select(col(ancestor.flag))
        .where(col(ancestor.flag).is_not(None), col(ancestor.id) == func.any(col(Region.path)))
        .order_by(func.cardinality(col(ancestor.path)).desc())
        .limit(1)
        .correlate(Region)
        .lateral("nearest_flag")
    )
    return (
        select(col(Region.id), nearest.c.flag)
        .select_from(Region)
        .join(nearest, true())
        .where(col(Region.id).in_(identities))
    )


class RegionService(DefinitionService[Region]):
    """Resolve flags for a bounded page without mutating stored region overrides."""

    async def resolve_flags(self, records: list[Region]) -> list[Region]:
        """Return presentation copies with the nearest ancestor's visual flag.

        Args:
            records: An authorized page or detail result from this request's service.

        Returns:
            Detached copies in input order. Stored nulls retain inheritance; reads
            and parent edits never copy inherited values into descendants.
        """
        if not records:
            return []
        # One MVCC snapshot resolves the whole page without taking write locks.
        rows = await self.session.exec(flag_query([record.id for record in records]))
        flags = dict(rows.all())
        return [record.model_copy(update={"flag": flags.get(record.id)}) for record in records]
