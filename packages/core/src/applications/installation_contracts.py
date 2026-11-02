"""Explicit configuration references shared by core and HTTP contracts."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SecretBinding(BaseModel):
    """Select an exact encrypted version and its declared confidential target."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version_id: UUID
    target: str = Field(min_length=1, max_length=512, pattern=r"^[^.\s]+(?:\.[^.\s]+)*$")
    use: str = Field(default="value", pattern=r"^value$")


class KubernetesSecretBinding(BaseModel):
    """Reference an existing Secret in the installation namespace, without its payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    namespace: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    name: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
    )
    key: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9._-]+$")
    target: str = Field(min_length=1, max_length=512, pattern=r"^[^.\s]+(?:\.[^.\s]+)*$")
    use: str = Field(default="existingSecret", pattern=r"^existingSecret$")
