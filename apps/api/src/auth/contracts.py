"""Public authentication DTOs without persistence or bearer session material."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, SecretStr, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError
from tinypods_api.http.contracts import RequestDTO
from tinypods_api.http.representations import Related, ResourceRead
from tinypods_api.http.versioning import API_V1_PREFIX
from tinypods_core.auth import Principal
from tinypods_core.auth.contracts import AccountName, EmailAddress, Password
from tinypods_core.db.models import User


class LoginRequest(RequestDTO):
    """Username and password for a local account."""

    username: AccountName
    password: Password


class PasswordConfirmationRequest(RequestDTO):
    """A password and matching confirmation."""

    password: Password
    password_confirmation: Password

    @field_validator("password_confirmation")
    @classmethod
    def confirm_password(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        """Check confirmation on the server without putting either secret in errors."""
        password = info.data.get("password")
        if (
            isinstance(password, SecretStr)
            and value.get_secret_value() != password.get_secret_value()
        ):
            message = "The passwords do not match."
            raise PydanticCustomError("password_mismatch", message, {"public_message": message})
        return value


class RegistrationRequest(PasswordConfirmationRequest):
    """Credentials for registering a local account."""

    username: AccountName


class OwnerSetupRequest(PasswordConfirmationRequest):
    """Email and credentials for initial owner setup."""

    email: EmailAddress


class SetupStatusResponse(BaseModel):
    """Whether the token-authorized setup form is available."""

    available: bool


class UserRead(ResourceRead):
    """Public signed-in account fields; credential records are separate."""

    resource_model = User
    id: UUID
    username: str
    is_owner: bool


class IdentityResponse(BaseModel):
    """The currently signed-in account as a public related representation."""

    user: Related[UserRead]

    @classmethod
    def from_principal(cls, principal: Principal, **fields: object) -> Self:
        """Copy an already verified identity without querying or exposing credentials."""
        return cls.model_validate(
            {
                "user": {
                    "id": principal.user_id,
                    "username": principal.username,
                    "is_owner": principal.is_owner,
                },
                **fields,
            }
        )


class RegistrationResponse(IdentityResponse):
    """A new account ready to sign in; registration does not issue a session."""


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
    """A sign-in method available for this instance."""

    id: str
    label: str
    type: Literal["password", "redirect"]
    icon_url: HttpUrl | None = None
    identity_providers: list[IdentityProviderDescriptor] | None = None
    login_path: str
    logout_path: str = API_V1_PREFIX + "/auth/logout"
    refresh_path: str | None = None
    registration_path: str | None = None


class ProvidersResponse(BaseModel):
    """Available sign-in methods."""

    data: list[ProviderDescriptor]
    registration_allowed: bool


class LoginStartRequest(RequestDTO):
    """The page to open after signing in."""

    action: Literal["login", "register"] = "login"
    return_to: str = Field(
        default="/",
        max_length=256,
        description="Relative API path or an absolute URL on a configured trusted browser origin.",
    )
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
