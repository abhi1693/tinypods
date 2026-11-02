"""Shared release validation issues and declared compatibility options."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReleaseValidationIssue(BaseModel):
    """A safe failure code and field path, never rejected input values."""

    model_config = ConfigDict(frozen=True)
    code: str
    path: list[str | int] = Field(default_factory=list)


class ReleaseOptions(BaseModel):
    """Operator-declared compatibility, independent of application visibility."""

    model_config = ConfigDict(extra="forbid", strict=True)
    architectures: list[Literal["amd64", "arm64", "arm", "ppc64le", "s390x", "riscv64"]] = Field(
        default_factory=list, max_length=6
    )
    renderer: Literal["helm"] = "helm"
    renderer_contract: Literal[1] = 1
    core_version_min: str | None = Field(default=None, max_length=128)
    core_version_max: str | None = Field(default=None, max_length=128)
