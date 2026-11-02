"""Small typed definitions for Helm application metadata; no runtime execution."""

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator
from tinypods_core.json_types import JsonRecord, JsonValue


class ApplicationStatus(StrEnum):
    """One operator-selected application classification, without policy enforcement."""

    EXPERIMENTAL = "experimental"
    STABLE = "stable"
    PREMIUM = "premium"


class ApplicationState(StrEnum):
    """Application visibility lifecycle, independent of maturity and release states."""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"


class ReleaseState(StrEnum):
    """Release availability lifecycle, free to evolve independently of applications."""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"


class SourceType(StrEnum):
    """Supported Helm chart sources."""

    HELM_REPOSITORY = "helm_repository"
    OCI_REGISTRY = "oci_registry"


class ConfigurationField(BaseModel):
    """Describe one Helm value without storing installation values or secrets.

    Chart defaults leave the chart untouched. Supply installation-specific values
    elsewhere; sensitive definitions cannot contain a shared literal value.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    label: str = Field(min_length=1, pattern=r"\S")
    type: str = Field(pattern=r"^(text|password|number|boolean|choice)$")
    set_by: str = Field(
        default="chart_default", pattern=r"^(chart_default|user|administrator|auto)$"
    )
    path: str = Field(pattern=r"^[^.\s]+(?:\.[^.\s]+)*$")
    value: JsonValue = None
    choices: list[JsonValue] | None = None
    install_only: bool = False
    sensitive: bool = False
    required: bool = False

    @model_validator(mode="after")
    def protect_sensitive_value(self) -> "ConfigurationField":
        """Prevent shared secret literals and choices on non-choice fields."""
        if (self.sensitive or self.type == "password") and self.value is not None:
            raise ValueError("Sensitive values belong to an installation, not a release")
        if self.type != "choice" and self.choices is not None:
            raise ValueError("Only choice fields accept choices")
        return self


class ConfigurationFields(TypeDecorator[list[JsonRecord]]):
    """Validate detached configuration definitions on every SQLAlchemy bind.

    Replace the list to persist edits; in-place JSON mutations are not tracked.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: object, dialect: Dialect) -> list[JsonRecord]:
        """Return finite, validated JSON definitions for persistence.

        Args:
            value: Configuration field definitions owned by the release.
            dialect: SQLAlchemy database dialect.

        Returns:
            Detached JSON definitions with explicit defaults.
        """
        fields = TypeAdapter(list[ConfigurationField]).validate_python(value, strict=True)
        encoded = [field.model_dump(mode="json") for field in fields]
        json.dumps(encoded, allow_nan=False)
        return TypeAdapter(list[JsonRecord]).validate_python(encoded, strict=True)
