"""Typed external source metadata, safe failures and base-URL validation."""

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

CHART_LAYER = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"


CHART_CONFIG = "application/vnd.cncf.helm.config.v1+json"


MANIFEST = "application/vnd.oci.image.manifest.v1+json"


CHART_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"


class SourceError(Exception):
    """Expose a fixed safe source failure, never upstream bodies, URLs or tokens."""

    def __init__(self, status: int, code: str) -> None:
        """Retain the deliberate HTTP classification and fixed machine-readable code."""
        self.status, self.code = status, code
        super().__init__(code)


def repository_url(source_type: str, value: str) -> str:
    """Validate an external source base URL without accepting credentials or an index path."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid repository URL") from None
    scheme = "https" if source_type == "helm_repository" else "oci"
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(segment in (".", "..") for segment in unquote(parsed.path).split("/"))
        or parsed.path.rstrip("/").endswith("/index.yaml")
    ):
        raise ValueError("Use a credential-free external repository base URL")
    return value.rstrip("/")


class Observation(BaseModel):
    """A version and package digest reported by upstream metadata, not a signature proof."""

    model_config = ConfigDict(frozen=True, strict=True)
    chart: str = Field(pattern=CHART_NAME, max_length=255)
    version: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SourceSnapshot:
    """Complete observed metadata to apply atomically after all source requests succeed."""

    versions: tuple[Observation, ...]
    discovery_supported: bool
