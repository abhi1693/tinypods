"""Local credentials and identity contracts independent of HTTP and persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    field_validator,
)
from pydantic_core import PydanticCustomError


def credential_feedback(value: object, handler: ValidatorFunctionWrapHandler) -> object:
    """Attach public credential-field feedback at its validation source."""
    try:
        result: object = handler(value)
        return result
    except ValidationError as exc:
        error = exc.errors()[0]
        context = error.get("ctx") or {}
        kind = error["type"]
        if value == "":
            message = "This field is required."
        elif len(exc.errors()) > 1:
            kind = "account_name_invalid"
            message = "Enter a valid username or email address."
        elif kind in {"string_too_short", "too_short"}:
            message = f"Use at least {context['min_length']} characters."
        elif kind in {"string_too_long", "too_long"}:
            message = f"Use no more than {context['max_length']} characters."
        else:
            message = "Enter a valid value."
        raise PydanticCustomError(kind, message, {**context, "public_message": message}) from None


Username = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")]
EmailAddress = Annotated[EmailStr, Field(max_length=254), WrapValidator(credential_feedback)]
AccountName = Annotated[Username | EmailAddress, WrapValidator(credential_feedback)]
Password = Annotated[
    SecretStr,
    Field(min_length=15, max_length=128),
    WrapValidator(credential_feedback),
]


class Credentials(BaseModel):
    """Validate ASCII account names and bounded passwords without logging secrets."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: AccountName
    password: Password

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
    refresh_token: str | None = field(default=None, repr=False)
    refresh_expires_at: datetime | None = None


class Scope(StrEnum):
    """List supported service authorization boundaries; unknown scopes fail closed."""

    APP = "app"
    CLUSTER = "cluster"
    SECRET = "secret"
    CATALOG_ADMIN = "catalog_admin"


@dataclass(frozen=True)
class VerifiedIdentity:
    """Carry verified identity and server-only session state without profile-based privileges."""

    issuer: str
    subject: str
    session_id: str | None = field(default=None, compare=False, repr=False)
    refresh_token: SecretStr | None = field(default=None, compare=False, repr=False)
    nonce: str | None = field(default=None, compare=False, repr=False)
    auth_time: float | None = field(default=None, compare=False, repr=False)
    issued_at: float | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class LogoutNotice:
    """Carry a validated provider logout event without retaining the signed token."""

    issuer: str
    client_id: str
    event_id: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    subject: str | None = field(default=None, repr=False)
    session_id: str | None = field(default=None, repr=False)
