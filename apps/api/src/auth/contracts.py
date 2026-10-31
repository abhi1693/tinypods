"""Public authentication DTOs without persistence or bearer session material."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr
from tinypods_api.http.contracts import RequestDTO


class LoginRequest(RequestDTO):
    """Accept bounded local credentials; passwords stay masked in diagnostics."""

    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: SecretStr = Field(min_length=15, max_length=128)


class IdentityResponse(BaseModel):
    """Expose only the authenticated account's normalized identity."""

    user_id: UUID
    username: str
    is_owner: bool


class LoginResponse(IdentityResponse):
    """Report absolute session expiration; the bearer token is in an HttpOnly cookie."""

    expires_at: datetime


class LogoutResponse(BaseModel):
    """Confirm durable session revocation."""

    logged_out: bool = True


class ProviderDescriptor(BaseModel):
    """Expose only enabled login methods and same-origin actions, never provider secrets/config."""

    id: str
    label: str
    type: Literal["password", "redirect"]
    login_path: str
    logout_path: str = "/auth/logout"


class ProvidersResponse(BaseModel):
    """List configured enabled methods without contacting an upstream identity provider."""

    data: list[ProviderDescriptor]


class LoginStartRequest(RequestDTO):
    """Accept only a bounded local post-login path."""

    return_to: str = Field(default="/", max_length=256)


class LoginStartResponse(BaseModel):
    """Carry one fresh authorization challenge; transaction secrets remain server-side."""

    authorization_url: str


class ProviderLogoutResponse(BaseModel):
    """Confirm application revocation and expose an optional token-free upstream logout target."""

    logged_out: bool = True
    logout_url: str | None
