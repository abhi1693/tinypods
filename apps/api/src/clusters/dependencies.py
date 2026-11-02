"""Request-owned registry services with shared authorization."""

from tinypods_api.auth import AuthorizationDependency
from tinypods_api.clusters.contracts import ClusterRead, RegionRead
from tinypods_api.http.dependencies import Session
from tinypods_api.http.presentation import ResourceService
from tinypods_core.applications.service import DefinitionService
from tinypods_core.clusters import Cluster, Region
from tinypods_core.clusters.service import RegionService


def regions(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[Region, RegionRead]:
    """Compose region metadata and inherited flag projection."""
    return ResourceService(RegionService(Region, session, authorization), RegionRead)


def clusters(
    session: Session, authorization: AuthorizationDependency
) -> ResourceService[Cluster, ClusterRead]:
    """Compose cluster registration without credential or network access."""
    return ResourceService(DefinitionService(Cluster, session, authorization), ClusterRead)
