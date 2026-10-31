"""Public authentication DTOs without persistence or bearer session material."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, SecretStr
from tinypods_api.http.contracts import RequestDTO


class LoginRequest(RequestDTO):
    """Username and password for a local account."""

    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: SecretStr = Field(min_length=15, max_length=128)


class IdentityResponse(BaseModel):
    """The currently signed-in account."""

    user_id: UUID
    username: str
    is_owner: bool


class LoginResponse(IdentityResponse):
    """Signed-in account details and sign-in expiry."""

    expires_at: datetime
    refresh_expires_at: datetime | None = None


class LogoutResponse(BaseModel):
    """Whether this browser has been signed out."""

    logged_out: bool = True


class IdentityProviderDescriptor(BaseModel):
    """An external sign-in option available through the configured authentication provider."""

    id: str
    type: str
    label: str
    icon_url: HttpUrl | None = None


class ProviderDescriptor(BaseModel):
    """A sign-in method available for this installation."""

    id: str
    label: str
    type: Literal["password", "redirect"]
    icon_url: HttpUrl | None = None
    identity_providers: list[IdentityProviderDescriptor] | None = None
    login_path: str
    logout_path: str = "/auth/logout"
    refresh_path: str | None = None


class ProvidersResponse(BaseModel):
    """Available sign-in methods."""

    data: list[ProviderDescriptor]


class LoginStartRequest(RequestDTO):
    """The page to open after signing in."""

    return_to: str = Field(default="/", max_length=256)
    identity_provider_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,255}$")


class LoginStartResponse(BaseModel):
    """A link to open to sign in."""

    authorization_url: str


class ProviderLogoutResponse(BaseModel):
    """Sign-out result and an optional link to finish external sign-out."""

    logged_out: bool = True
    logout_url: str | None


class BackchannelErrorResponse(BaseModel):
    """A sign-out notification that could not be verified."""

    error: Literal["invalid_request"] = "invalid_request"
