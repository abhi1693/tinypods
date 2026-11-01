"""Write-only confidential inputs and nested metadata-only responses."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from tinypods_api.clusters.contracts import ClusterRead
from tinypods_api.http import PageParams, RequestDTO
from tinypods_api.http.model_contracts import DefinitionRead
from tinypods_api.http.representations import Related
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


class ClusterSecretWrite(RequestDTO):
    """Create a cluster's self-contained kubeconfig; the document is never returned."""

    cluster_id: UUID
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = Field(default="", max_length=10000)
    kubeconfig: SecretStr = Field(min_length=1, max_length=262144)


class SecretVersionWrite(RequestDTO):
    """Append a credential version, optionally staging it without activation."""

    secret_id: UUID
    kubeconfig: SecretStr = Field(min_length=1, max_length=262144)

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
