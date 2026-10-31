"""Validated process configuration; connection adapters are added by their own features."""

from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(ValueError):
    """A configuration error safe to display in process logs."""


class IdentityProviderSettings(BaseModel):
    """Describe one configured upstream sign-in option for frontend display and selection."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,255}$")
    type: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    icon_url: HttpUrl | None = None


class Settings(BaseSettings):
    """Validate process configuration loaded from TINYPODS_ environment variables.

    Secret URL fields hide connection values in model representations. Cache
    configuration is optional; disabling it does not change database or queue
    ownership. Cache-specific connection settings can override the shared endpoint.

    Attributes:
        valkey_url: Shared private Valkey endpoint for runtime services.
        cache_enabled: Whether explicitly annotated public reads may use storage.
        cache_valkey_url: Optional dedicated cache endpoint instead of valkey_url.
        cache_namespace: Operator-controlled prefix separating deployment environments.
        cache_ttl_seconds: Bounded lifetime for content responses.
        cache_metadata_ttl_seconds: Bounded lifetime for metadata responses.
        cache_max_bytes: Maximum serialized body accepted into the response cache.
    """

    model_config = SettingsConfigDict(env_prefix="TINYPODS_", hide_input_in_errors=True)

    environment: Literal["production", "development"] = "production"
    service_name: str = Field(default="TinyPods", min_length=1, max_length=80)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    log_queue_capacity: int = Field(default=1024, ge=1, le=65536)
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    readiness_timeout: float = Field(default=2.0, gt=0, le=30)
    database_url: SecretStr | None = None
    migration_database_url: SecretStr | None = None
    database_mode: Literal["direct", "pgbouncer"] = "direct"
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    database_pool_timeout: float = Field(default=5.0, gt=0, le=60)
    database_connect_timeout: int = Field(default=5, ge=1, le=60)
    valkey_url: SecretStr | None = None
    rate_limit_global_per_minute: int = Field(default=0, ge=0, le=1_000_000)
    rate_limit_valkey_url: SecretStr | None = None
    rate_limit_namespace: str = Field(default="tinypods:limits", pattern=r"^[a-zA-Z0-9:_-]{1,80}$")
    cache_enabled: bool = True
    cache_valkey_url: SecretStr | None = None
    cache_namespace: str = Field(default="tinypods:cache", pattern=r"^[a-zA-Z0-9:_-]{1,80}$")
    cache_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    cache_metadata_ttl_seconds: int = Field(default=600, ge=1, le=3600)
    cache_max_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)
    auth_local_label: str = Field(default="Username and password", min_length=1, max_length=80)
    auth_local_icon_url: HttpUrl | None = None
    zitadel_label: str = Field(default="Zitadel", min_length=1, max_length=80)
    zitadel_icon_url: HttpUrl | None = None
    auth_local_enabled: bool = True
    zitadel_enabled: bool = False
    zitadel_refresh_enabled: bool = False
    zitadel_refresh_encryption_keys: tuple[SecretStr, ...] = Field(default=(), max_length=5)
    zitadel_refresh_max_age_seconds: int = Field(default=2_592_000, ge=3600, le=7_776_000)
    zitadel_issuer: str | None = None
    zitadel_organization_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,255}$")
    zitadel_organization_domain: str | None = Field(
        default=None,
        min_length=1,
        max_length=253,
        pattern=r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    )
    zitadel_identity_providers: tuple[IdentityProviderSettings, ...] = Field(
        default=(), max_length=32
    )
    zitadel_client_id: str | None = Field(default=None, min_length=1, max_length=255)
    zitadel_client_secret: SecretStr | None = None
    zitadel_token_auth_method: Literal["none", "client_secret_basic"] = "none"
    zitadel_redirect_uri: str | None = None
    zitadel_post_logout_uri: str | None = None

    @field_validator("zitadel_refresh_encryption_keys")
    @classmethod
    def validate_refresh_keys(cls, values: tuple[SecretStr, ...]) -> tuple[SecretStr, ...]:
        """Require supported authenticated-encryption keys without exposing their values."""
        try:
            for value in values:
                Fernet(value.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid refresh-token encryption key") from None
        return values

    @field_validator("zitadel_issuer", "zitadel_redirect_uri", "zitadel_post_logout_uri")
    @classmethod
    def validate_oidc_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Require HTTPS, allowing development HTTP browser redirects."""
        if value is None:
            return None
        try:
            url = urlsplit(value)
            _ = url.port
        except ValueError:
            raise ValueError("Invalid OIDC URL") from None
        if (
            (
                url.scheme != "https"
                and not (
                    info.data.get("environment") == "development"
                    and info.field_name != "zitadel_issuer"
                    and url.scheme == "http"
                )
            )
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or "\\" in value
        ):
            raise ValueError("Invalid OIDC URL")
        return value

    @field_validator("zitadel_organization_domain")
    @classmethod
    def normalize_organization_domain(cls, value: str | None) -> str | None:
        """Use a canonical lowercase DNS domain for organization selection."""
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def validate_oidc_configuration(self) -> "Settings":
        """Reject incomplete enabled providers instead of silently enabling another login method."""
        if (
            self.zitadel_organization_id is not None
            and self.zitadel_organization_domain is not None
        ):
            raise ValueError("Configure an organization ID or domain, not both")
        if self.zitadel_identity_providers and not (
            self.zitadel_organization_id or self.zitadel_organization_domain
        ):
            raise ValueError("A selected Zitadel identity provider requires an organization")
        provider_ids = [provider.id for provider in self.zitadel_identity_providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("Zitadel identity provider IDs must be unique")
        if self.zitadel_refresh_enabled and not self.zitadel_refresh_encryption_keys:
            raise ValueError("Refresh support requires an operator encryption key")
        if self.zitadel_enabled:
            if (
                not self.zitadel_issuer
                or not self.zitadel_client_id
                or not self.zitadel_redirect_uri
            ):
                raise ValueError("Enabled Zitadel requires issuer, client ID and callback")
            if self.zitadel_issuer.endswith("/"):
                raise ValueError("Use the exact canonical issuer without a trailing slash")
            if not self.zitadel_redirect_uri.endswith("/auth/providers/zitadel/callback"):
                raise ValueError("Zitadel callback must use the supported callback path")
            if (
                self.zitadel_token_auth_method == "client_secret_basic"
                and not self.zitadel_client_secret
            ):
                raise ValueError("Confidential Zitadel clients require a secret")
        return self

    worker_heartbeat_interval: float = Field(default=30.0, gt=0, le=60)

    @field_validator("valkey_url", "cache_valkey_url", "rate_limit_valkey_url")
    @classmethod
    def validate_valkey_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the scheme, hostname and optional port of a Valkey endpoint.

        Args:
            value: Secret redis/rediss URL, or None when no endpoint is configured.

        Returns:
            The unchanged secret URL, or None.

        Raises:
            ValueError: The URL lacks a supported scheme/host or has an invalid port.
        """
        if value is None:
            return None
        try:
            url = urlsplit(value.get_secret_value())
            _ = url.port
        except ValueError:
            raise ValueError("Invalid Valkey URL") from None
        if url.scheme not in {"redis", "rediss"} or not url.hostname:
            raise ValueError("Invalid Valkey URL")
        return value

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate supported PostgreSQL drivers while retaining secret-valued URLs.

        Args:
            value: Secret PostgreSQL URL, or None when database access is disabled.

        Returns:
            The unchanged secret endpoint, or None when not configured.

        Raises:
            ValueError: The configured URL is malformed or uses an unsupported driver.
        """
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
            if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                raise ValueError("PostgreSQL with psycopg is required")
        except ArgumentError:
            raise ValueError("Invalid PostgreSQL URL") from None
        return value


def load_settings() -> Settings:
    """Validate environment settings and report field names without rejected values.

    Returns:
        Validated deployment settings.

    Raises:
        ConfigurationError: Deployment settings fail validation.
    """
    try:
        return Settings()
    except ValidationError as exc:
        fields = sorted(
            {(str(error["loc"][0]) if error["loc"] else "authentication") for error in exc.errors()}
        )
        raise ConfigurationError("Invalid settings: " + ", ".join(fields)) from None
