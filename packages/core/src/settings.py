"""Validated process configuration; connection adapters are added by their own features."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(ValueError):
    """A configuration error safe to display in process logs."""


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
    cache_enabled: bool = True
    cache_valkey_url: SecretStr | None = None
    cache_namespace: str = Field(default="tinypods:cache", pattern=r"^[a-zA-Z0-9:_-]{1,80}$")
    cache_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    cache_metadata_ttl_seconds: int = Field(default=600, ge=1, le=3600)
    cache_max_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)
    worker_heartbeat_interval: float = Field(default=30.0, gt=0, le=60)

    @field_validator("valkey_url", "cache_valkey_url")
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
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise ConfigurationError("Invalid settings: " + ", ".join(fields)) from None
