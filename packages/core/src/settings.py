"""Validated process configuration; connection adapters are added by their own features."""

from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    valkey_url: SecretStr | None = None

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value.get_secret_value()
            for value in (self.database_url, self.valkey_url)
            if value is not None and value.get_secret_value()
        )


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise ConfigurationError("Invalid settings: " + ", ".join(fields)) from None
