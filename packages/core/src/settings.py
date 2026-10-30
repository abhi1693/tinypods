"""Validated process configuration; connection adapters are added by their own features."""

from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(ValueError):
    """A configuration error safe to display in process logs."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TINYPODS_", hide_input_in_errors=True)

    service_name: str = Field(default="TinyPods", min_length=1, max_length=80)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_host: str = "127.0.0.1"
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

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
            if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                raise ValueError("PostgreSQL with psycopg is required")
        except ArgumentError:
            raise ValueError("Invalid PostgreSQL URL") from None
        return value

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value.get_secret_value()
            for value in (self.database_url, self.migration_database_url, self.valkey_url)
            if value is not None and value.get_secret_value()
        )


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise ConfigurationError("Invalid settings: " + ", ".join(fields)) from None
