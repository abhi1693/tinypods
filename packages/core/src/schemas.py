"""Public data contracts stay independent of persistence model fields."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class InstallationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    installation_id: UUID
    created_at: datetime
