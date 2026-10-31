"""Built-in provider router composition."""

from tinypods_api.auth.providers.local import router as local_router
from tinypods_api.auth.providers.zitadel import router as zitadel_router

__all__ = ["local_router", "zitadel_router"]
