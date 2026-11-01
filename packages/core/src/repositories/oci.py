"""OCI chart discovery and anonymous registry authentication."""

import base64
import hashlib
import re
from typing import Literal
from urllib.parse import urlencode, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from tinypods_core.repositories.sources import (
    CHART_CONFIG,
    CHART_LAYER,
    CHART_NAME,
    MANIFEST,
    Observation,
    SourceError,
    SourceSnapshot,
)
from tinypods_core.repositories.transport import Document, MetadataHTTP


class Descriptor(BaseModel):
    """OCI descriptor restricted to the digest and media type needed here."""

    model_config = ConfigDict(strict=True)
    mediaType: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Manifest(BaseModel):
    """OCI manifest metadata; chart archive layers are never downloaded during sync."""

    model_config = ConfigDict(strict=True)
    schemaVersion: Literal[2]
    config: Descriptor
    layers: list[Descriptor]


class ChartConfig(BaseModel):
    """Name/version in the small OCI Helm config blob."""

    model_config = ConfigDict(strict=True)
    name: str
    version: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=10000)
    icon: str | None = Field(default=None, max_length=2048)


class Names(BaseModel):
    """Registry discovery response."""

    model_config = ConfigDict(strict=True)
    repositories: list[str]


class Tags(BaseModel):
    """A bounded page of tags for one explicitly named OCI repository."""

    model_config = ConfigDict(strict=True)
    tags: list[str] | None


class Token(BaseModel):
    """Ephemeral anonymous registry credentials; representations hide token values."""

    token: SecretStr | None = None
    access_token: SecretStr | None = None


class OCIRepositoryClient:
    """Discover known OCI chart paths and read tags, manifests and small config blobs."""

    def __init__(self, http: MetadataHTTP) -> None:
        """Retain the shared bounded HTTPS policy."""
        self.http = http

    async def registry_get(self, url: str, scope: str) -> Document:
        """Support public registries' anonymous Bearer challenge without stored credentials."""
        document = await self.http.get(url, headers={"Accept": MANIFEST})
        if document.status != 401:
            return document
        challenge = document.headers.get("www-authenticate", "")
        if not challenge.lower().startswith("bearer "):
            return document
        parameters = {
            key.casefold(): value
            for key, value in re.findall(r'([A-Za-z]+)="([^"]*)"', challenge[7:])
        }
        realm = parameters.get("realm")
        if not realm:
            raise SourceError(502, "invalid_registry_authentication")
        token_url = (
            realm
            + ("&" if "?" in realm else "?")
            + urlencode({"service": parameters.get("service", ""), "scope": scope})
        )
        credentials = self.http.settings.repository_credentials.get(urlsplit(url).hostname or "")
        token_headers: dict[str, str] = {}
        if credentials is not None:
            # Token challenges are untrusted metadata. Forward account credentials only
            # to the registry itself or its explicitly configured authentication host.
            realm_host = urlsplit(realm).hostname
            if realm_host not in (urlsplit(url).hostname, credentials.token_host):
                raise SourceError(403, "untrusted_authentication_host")
            raw = (
                credentials.username.get_secret_value()
                + ":"
                + credentials.password.get_secret_value()
            )
            token_headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode(
                "ascii"
            )
        token_document = await self.http.get(token_url, headers=token_headers, authenticate=False)
        if token_document.status != 200:
            raise SourceError(502, "source_authentication_failed")
        try:
            parsed = Token.model_validate_json(token_document.body)
        except ValidationError:
            raise SourceError(502, "source_authentication_failed") from None
        token = parsed.token or parsed.access_token
        if token is None or not token.get_secret_value() or len(token.get_secret_value()) > 8192:
            raise SourceError(502, "source_authentication_failed")
        return await self.http.get(
            url, headers={"Accept": MANIFEST, "Authorization": "Bearer " + token.get_secret_value()}
        )

    async def names(self, url: str, field: str, scope: str) -> list[str] | None:
        """Follow validated registry pagination; absent catalog discovery is explicit."""
        values: list[str] = []
        for _ in range(100):
            document = await self.registry_get(url, scope)
            if document.status in (401, 403, 404, 405) and field == "repositories":
                return None
            if document.status == 404 and field == "tags":
                return []
            if document.status != 200:
                raise SourceError(502, "source_unavailable")
            try:
                parsed = (
                    Names.model_validate_json(document.body).repositories
                    if field == "repositories"
                    else Tags.model_validate_json(document.body).tags
                )
            except ValidationError:
                raise SourceError(422, "invalid_source_metadata") from None
            values.extend(parsed or [])
            if len(values) > self.http.settings.repository_max_items:
                raise SourceError(422, "source_item_limit")
            link = document.headers.get("link")
            if not link:
                return values
            match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="?next"?\s*', link)
            if not match:
                raise SourceError(422, "invalid_source_metadata")
            following = urljoin(document.url, match[1])
            if urlsplit(following).netloc != urlsplit(url).netloc:
                raise SourceError(403, "source_not_allowed")
            url = following
        raise SourceError(422, "source_page_limit")

    async def fetch(self, url: str, known_charts: tuple[str, ...]) -> SourceSnapshot:
        """Read Helm metadata, using registered names if registry-wide discovery is unavailable."""
        parsed = urlsplit(url)
        origin = "https://" + parsed.netloc
        namespace = parsed.path.strip("/")
        catalog = await self.names(
            origin + "/v2/_catalog?n=100", "repositories", "registry:catalog:*"
        )
        prefix = namespace + "/" if namespace else ""
        discovered = [name[len(prefix) :] for name in (catalog or []) if name.startswith(prefix)]
        # Registry-wide catalog discovery is optional. Registered chart paths keep
        # tag/version synchronization usable on public registries that hide the catalog.
        charts = sorted(set(discovered) | set(known_charts))
        if catalog is None and not charts:
            raise SourceError(409, "chart_discovery_unavailable")
        observations = []
        for chart in charts:
            if len(chart) > 255 or not re.fullmatch(CHART_NAME, chart):
                raise SourceError(422, "invalid_source_metadata")
            name = prefix + chart
            scope = "repository:" + name + ":pull"
            base = origin + "/v2/" + name
            tags = await self.names(base + "/tags/list?n=100", "tags", scope)
            for tag in tags or []:
                if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
                    raise SourceError(422, "invalid_source_metadata")
                document = await self.registry_get(base + "/manifests/" + tag, scope)
                if document.status != 200:
                    raise SourceError(502, "source_unavailable")
                try:
                    manifest = Manifest.model_validate_json(document.body)
                except ValidationError:
                    raise SourceError(422, "invalid_source_metadata") from None
                if manifest.config.mediaType != CHART_CONFIG:
                    continue
                layers = [layer for layer in manifest.layers if layer.mediaType == CHART_LAYER]
                if len(layers) != 1:
                    raise SourceError(422, "invalid_source_metadata")
                # The config supplies the real Helm name/version. Verify its descriptor
                # digest before trusting it; archive bytes remain entirely upstream.
                config_document = await self.registry_get(
                    base + "/blobs/" + manifest.config.digest, scope
                )
                if (
                    config_document.status != 200
                    or "sha256:" + hashlib.sha256(config_document.body).hexdigest()
                    != manifest.config.digest
                ):
                    raise SourceError(422, "source_digest_mismatch")
                try:
                    config = ChartConfig.model_validate_json(config_document.body)
                except ValidationError:
                    raise SourceError(422, "invalid_source_metadata") from None
                if (
                    config.name != chart.rsplit("/", 1)[-1]
                    or config.version.replace("+", "_") != tag
                    or config.version.lower() == "latest"
                ):
                    raise SourceError(422, "invalid_source_metadata")
                observations.append(
                    Observation(
                        chart=chart,
                        version=config.version,
                        digest=layers[0].digest,
                        description=config.description,
                        icon=config.icon,
                    )
                )
                if len(observations) > self.http.settings.repository_max_items:
                    raise SourceError(422, "source_item_limit")
        return SourceSnapshot(tuple(observations), catalog is not None)
