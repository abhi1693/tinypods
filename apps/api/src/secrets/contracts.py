"""Write-only confidential inputs and nested metadata-only responses."""

import base64
import binascii
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, SecretStr, model_validator
from tinypods_api.clusters.contracts import ClusterRead
from tinypods_api.http import RequestDTO
from tinypods_api.http.model_contracts import DefinitionRead
from tinypods_api.http.representations import Related, ResourceRead
from tinypods_core.secrets.crypto import MAX_SECRET_BYTES
from tinypods_core.secrets.models import ClusterSecret, SecretVersion


class KubeconfigVersionRead(DefinitionRead):
    """One cluster's version metadata; storage-level secret identities remain internal."""

    resource_model = SecretVersion
    version: int
    encryption_key_id: str
    revoked_at: datetime | None


class KubeconfigRead(ResourceRead):
    """Cluster-focused credential metadata without a separate secret resource to manage."""

    resource_model = ClusterSecret
    created: datetime
    last_updated: datetime
    cluster: Related[ClusterRead]
    active_version: Related[KubeconfigVersionRead] | None


class KubeconfigInput(RequestDTO):
    """Accept explicitly selected transport without exposing confidential input errors."""

    model_config = ConfigDict(hide_input_in_errors=True)
    encoding: Literal["plain", "base64"] = Field(
        default="plain", description="How kubeconfig is transported; no automatic detection."
    )
    kubeconfig: SecretStr = Field(
        min_length=1,
        max_length=4 * ((MAX_SECRET_BYTES + 2) // 3),
        description=(
            "UTF-8 YAML/JSON or standard Base64, selected by encoding; decoded limit 262144 bytes."
        ),
    )

    def decoded_payload(self) -> bytes:
        """Strictly decode the selected transport into bounded original UTF-8 bytes."""
        text = self.kubeconfig.get_secret_value()
        if self.encoding == "base64":
            try:
                payload = base64.b64decode(text, validate=True)
                # Canonical encoding rejects ambiguous padding and unused non-zero bits.
                if base64.b64encode(payload).decode("ascii") != text:
                    raise ValueError
            except (ValueError, binascii.Error):
                raise ValueError("Kubeconfig must be canonical Base64") from None
        else:
            try:
                payload = text.encode("utf-8")
            except UnicodeError:
                raise ValueError("Kubeconfig must be valid UTF-8") from None
        if not payload or len(payload) > MAX_SECRET_BYTES:
            raise ValueError("Decoded kubeconfig exceeds the byte limit or is empty")
        try:
            payload.decode("utf-8")
        except UnicodeError:
            raise ValueError("Decoded kubeconfig must be valid UTF-8") from None
        return payload

    @model_validator(mode="after")
    def validate_transport(self) -> "KubeconfigInput":
        """Reject malformed transport before service calls or persistence."""
        self.decoded_payload()
        return self


class KubeconfigWrite(KubeconfigInput):
    """Upload a kubeconfig version for the cluster named by the request path."""

    activate: bool = Field(
        default=True, description="Activate immediately; false stages this version."
    )


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
