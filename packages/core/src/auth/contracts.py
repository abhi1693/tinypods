"""Local credentials and identity contracts independent of HTTP and persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Credentials(BaseModel):
    """Validate ASCII account names and bounded passwords without logging secrets."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        """Return the case-insensitive canonical account name."""
        return value.lower()


class AuthError(Exception):
    """Carry a deliberate HTTP-compatible status without credential details."""

    def __init__(self, status: int, *, retry_after: int = 60) -> None:
        """Retain only the public failure status."""
        self.status = status
        self.retry_after = retry_after
        super().__init__(status)


@dataclass(frozen=True)
class Principal:
    """Normalize identity; services must revalidate its session before granting access."""

    user_id: UUID
    username: str
    is_owner: bool
    credential_id: str = field(repr=False)
    credential_kind: str = "session"


@dataclass(frozen=True)
class Login:
    """Return newly issued secrets once; persistence stores only their digests."""

    principal: Principal
    expires_at: datetime
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


class Scope(StrEnum):
    """List supported service authorization boundaries; unknown scopes fail closed."""

    APP = "app"
    CLUSTER = "cluster"
    SECRET = "secret"
    CATALOG_ADMIN = "catalog_admin"
