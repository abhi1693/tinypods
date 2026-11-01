"""Request-owned registry services with shared authorization."""

from tinypods_api.applications.dependencies import Session
from tinypods_api.auth import SessionDependency
from tinypods_core.applications.service import DefinitionService
from tinypods_core.auth.authorization import AuthorizationService
from tinypods_core.clusters import Cluster, Region
from tinypods_core.clusters.service import RegionService


def regions(session: Session, auth: SessionDependency) -> RegionService:
    """Compose region metadata and inherited flag projection."""
    return RegionService(Region, session, AuthorizationService(auth))


def clusters(session: Session, auth: SessionDependency) -> DefinitionService[Cluster]:
    """Compose cluster registration without credential or network access."""
    return DefinitionService(Cluster, session, AuthorizationService(auth))
