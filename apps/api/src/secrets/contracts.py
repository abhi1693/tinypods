"""Write-only confidential inputs and nested metadata-only responses."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, SecretStr, field_validator
from tinypods_api.clusters.contracts import ClusterRead
from tinypods_api.http import PageParams, RequestDTO
from tinypods_api.http.model_contracts import DefinitionRead
from tinypods_api.http.representations import Related
from tinypods_core.secrets.crypto import MAX_SECRET_BYTES
from tinypods_core.secrets.models import ClusterSecret, Secret, SecretVersion


class SecretRead(DefinitionRead):
    """Public secret metadata, without a confidential payload or encryption material."""

    resource_model = Secret
    name: str
    description: str
    kind: Literal["cluster"]


class SecretVersionRead(DefinitionRead):
    """Immutable logical version metadata and revocation state."""

    resource_model = SecretVersion
    secret: Related[SecretRead]
    version: int
    encryption_key_id: str
    revoked_at: datetime | None


class ClusterSecretRead(DefinitionRead):
    """Nested cluster association and the selected logical version's safe metadata."""

    resource_model = ClusterSecret
    secret: Related[SecretRead]
    cluster: Related[ClusterRead]
    active_version: Related[SecretVersionRead] | None


class KubeconfigInput(RequestDTO):
    """Keep confidential input errors private and bound the original UTF-8 bytes."""

    model_config = ConfigDict(hide_input_in_errors=True)
    kubeconfig: SecretStr = Field(min_length=1, max_length=MAX_SECRET_BYTES)

    @field_validator("kubeconfig")
    @classmethod
    def bounded_utf8(cls, value: SecretStr) -> SecretStr:
        """Reject unpaired surrogates and byte-size expansion before persistence."""
        try:
            payload = value.get_secret_value().encode()
        except UnicodeError:
            raise ValueError("Kubeconfig must be valid UTF-8") from None
        if len(payload) > MAX_SECRET_BYTES:
            raise ValueError("Kubeconfig exceeds the byte limit")
        return value


class ClusterSecretWrite(KubeconfigInput):
    """Create a cluster's self-contained kubeconfig; the document is never returned."""

    cluster_id: UUID
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = Field(default="", max_length=10000)


class SecretVersionWrite(KubeconfigInput):
    """Append a credential version, optionally staging it without activation."""

    secret_id: UUID

    activate: bool = Field(
        default=True, description="Activate this version immediately; false keeps it staged."
    )


class ActivateWrite(RequestDTO):
    """Select a non-revoked version that belongs to this association's secret."""

    version_id: UUID


class ClusterSecretQuery(PageParams):
    """Filter owner-only credential associations by their actual cluster ID."""

    cluster_id: UUID | None = None


class SecretVersionQuery(PageParams):
    """Filter version history by its actual shared secret ID."""

    secret_id: UUID | None = None


class ConnectionRead(RequestDTO):
    """Bounded connection diagnostics; upstream bodies and credentials are excluded."""

    connected: bool
    reason: Literal[
        "connected",
        "unauthorized",
        "forbidden",
        "invalid_response",
        "invalid_kubeconfig",
        "timeout",
        "unreachable",
    ]
    kubernetes_version: str | None = None
