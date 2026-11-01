"""Computed region presentation values shared by full and brief response schemas."""

from collections.abc import Sequence
from typing import cast

from sqlmodel.ext.asyncio.session import AsyncSession
from tinypods_api.http.representations import ComputedValues
from tinypods_core.clusters import Region
from tinypods_core.clusters.service import flag_query
from tinypods_core.persistence import CoreModel


async def region_values(
    session: AsyncSession, records: Sequence[CoreModel], fields: frozenset[str]
) -> ComputedValues:
    """Resolve inherited flags once for every requested region in the loaded graph."""
    if "flag" not in fields:
        return {}
    identities = [cast(Region, record).id for record in records]
    flags = dict((await session.exec(flag_query(identities))).all())
    return {(identity,): {"flag": flags.get(identity)} for identity in identities}
