"""Helm index parsing and metadata discovery without archive downloads."""

import asyncio

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from tinypods_core.repositories.sources import Observation, SourceError, SourceSnapshot
from tinypods_core.repositories.transport import MetadataHTTP


class IndexVersion(BaseModel):
    """Only index fields needed for exact chart/version selection."""

    model_config = ConfigDict(strict=True)
    name: str
    version: str = Field(min_length=1, max_length=128)
    digest: str
    description: str = Field(default="", max_length=10000)
    icon: str | None = Field(default=None, max_length=2048)

    @field_validator("digest")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        """Normalize the bare SHA-256 form used by Helm index files."""
        return value if value.startswith("sha256:") else "sha256:" + value


class Index(BaseModel):
    """A Helm repository index of named chart version entries."""

    model_config = ConfigDict(strict=True)
    entries: dict[str, list[IndexVersion]]


def parse_index(body: bytes, maximum: int) -> list[Observation]:
    """Parse bounded YAML off the event loop, rejecting aliases, deep nesting and excess entries."""
    try:
        # Reject aliases and excessive nesting before constructing Python objects.
        # SafeLoader blocks executable tags but alone does not bound document graphs.
        depth = 0
        for count, event in enumerate(yaml.parse(body)):
            if isinstance(event, yaml.events.AliasEvent) or count > maximum * 100 + 100:
                raise SourceError(422, "invalid_source_metadata")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise SourceError(422, "invalid_source_metadata")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        index = Index.model_validate(yaml.safe_load(body))
        result = []
        for name, versions in index.entries.items():
            for version in versions:
                if version.name != name or version.version.lower() == "latest":
                    raise SourceError(422, "invalid_source_metadata")
                result.append(
                    Observation(
                        chart=name,
                        version=version.version,
                        digest=version.digest,
                        description=version.description,
                        icon=version.icon,
                    )
                )
                if len(result) > maximum:
                    raise SourceError(422, "source_item_limit")
        return result
    except (yaml.YAMLError, ValidationError, UnicodeError, RecursionError):
        raise SourceError(422, "invalid_source_metadata") from None


class HelmRepositoryClient:
    """Read complete chart metadata from an external Helm repository index."""

    def __init__(self, http: MetadataHTTP) -> None:
        """Retain the shared bounded HTTPS policy."""
        self.http = http

    async def fetch(self, url: str) -> SourceSnapshot:
        """Fetch the index and parse bounded YAML away from the event loop."""
        document = await self.http.get(url + "/index.yaml")
        if document.status != 200:
            raise SourceError(502, "source_unavailable")
        observations = await asyncio.to_thread(
            parse_index, document.body, self.http.settings.repository_max_items
        )
        return SourceSnapshot(tuple(observations), True)
