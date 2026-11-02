"""Public intended-installation inputs and configuration history responses."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr
from tinypods_api.applications.contracts import ReleaseRead
from tinypods_api.auth.contracts import UserRead
from tinypods_api.clusters.contracts import ClusterRead
from tinypods_api.http import RequestDTO
from tinypods_api.http.model_contracts import DefinitionRead
from tinypods_api.http.representations import Related
from tinypods_core.applications.contracts import ReleaseValidationIssue
from tinypods_core.applications.installation_contracts import KubernetesSecretBinding, SecretBinding
from tinypods_core.applications.installations import (
    ApplicationInstallation,
    ApplicationSecret,
    InstallationRevision,
    RevisionKubernetesSecret,
    RevisionSecret,
)
from tinypods_core.json_types import JsonRecord
from tinypods_core.secrets.models import Secret, SecretVersion


class InstallationCreate(RequestDTO):
    """Reserve an intended target; no configuration is deployed."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    cluster_id: UUID
    namespace: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class InstallationPatch(RequestDTO):
    """Only the descriptive name is writable after creation."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")


class InstallationMetadataRead(DefinitionRead):
    """Installation identity used in scoped related representations."""

    resource_model = ApplicationInstallation
    name: str
    namespace: str


class ConfigurationInput(RequestDTO):
    """Declared choices and scoped references, excluding confidential literals."""

    release_id: UUID
    overrides: JsonRecord = Field(default_factory=dict, repr=False)
    secrets: list[SecretBinding] = Field(default_factory=list, max_length=100)
    kubernetes_secrets: list[KubernetesSecretBinding] = Field(default_factory=list, max_length=100)


class ConfigurationRead(BaseModel):
    """Structural configuration results; Kubernetes objects are not queried."""

    valid: bool
    issues: list[ReleaseValidationIssue]


class ApplicationSecretCreate(RequestDTO):
    """Write-only UTF-8 secret payload; ordinary responses return metadata only."""

    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    payload: SecretStr = Field(min_length=1, max_length=262144)


class ApplicationSecretVersionCreate(RequestDTO):
    """Append a logical secret version without an automatic active pointer."""

    payload: SecretStr = Field(min_length=1, max_length=262144)


class SecretMetadataRead(DefinitionRead):
    """Safe shared secret identity; encrypted payloads are never projected."""

    resource_model = Secret
    name: str
    description: str
    kind: str


class ApplicationSecretRead(DefinitionRead):
    """Actual installation association without encrypted or plaintext columns."""

    resource_model = ApplicationSecret
    installation: Related[InstallationMetadataRead]
    secret: Related[SecretMetadataRead]


class SecretVersionRead(DefinitionRead):
    """Safe logical version metadata; exact identities survive key rotation."""

    resource_model = SecretVersion
    secret: Related[SecretMetadataRead]
    version: int
    encryption_key_id: str
    revoked_at: datetime | None


class RevisionSecretRead(DefinitionRead):
    """Exact logical secret version and its configuration target."""

    resource_model = RevisionSecret
    version: Related[SecretVersionRead]
    target: str
    use: str


class KubernetesSecretRead(DefinitionRead):
    """Separate scoped Kubernetes reference; no payload or live-readiness claim."""

    resource_model = RevisionKubernetesSecret
    name: str
    key: str
    target: str
    use: str


class RevisionRead(DefinitionRead):
    """Immutable settings and exact references, excluding confidential payloads."""

    resource_model = InstallationRevision
    installation: Related[InstallationMetadataRead]
    release: Related[ReleaseRead]
    overrides: JsonRecord = Field(repr=False)
    values: JsonRecord = Field(repr=False)
    secrets: list[RevisionSecretRead]
    kubernetes_secrets: list[KubernetesSecretRead]


class InstallationRead(InstallationMetadataRead):
    """Target metadata; null revision means no deployed configuration."""

    owner: Related[UserRead]
    cluster: Related[ClusterRead]
    revision: Related[RevisionRead] | None
