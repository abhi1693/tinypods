"""Bounded, non-executing inspection of digest-verified Helm archives."""

import gzip
import hashlib
import io
import json
import math
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml  # type: ignore[import-untyped]
from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.repositories.sources import SourceError

MAX_ARCHIVE_BYTES = 16_000_000
MAX_EXPANDED_BYTES = 64_000_000
MAX_DOCUMENT_BYTES = 2_000_000
MAX_FILES = 4096
MAX_DEPTH = 32


@dataclass(frozen=True)
class ChartFacts:
    """Verified identity and configuration facts; never an executable chart."""

    name: str
    version: str
    digest: str
    archive_bytes: int
    files: int
    metadata: JsonRecord
    defaults: JsonRecord
    values_schema: JsonRecord | None


def json_value(value: object, depth: int = 0) -> JsonValue:
    """Reject non-JSON YAML types and deep configuration without echoing values."""
    if depth > MAX_DEPTH:
        raise SourceError(422, "invalid_chart_document")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [json_value(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: json_value(item, depth + 1) for key, item in value.items()}
    raise SourceError(422, "invalid_chart_document")


def document(body: bytes, *, schema: bool = False, allow_empty: bool = False) -> JsonRecord:
    """Parse one bounded mapping, rejecting aliases, duplicate keys and executable tags."""
    if len(body) > MAX_DOCUMENT_BYTES:
        raise SourceError(422, "chart_document_too_large")
    try:
        # Parse events first: SafeLoader alone still permits alias expansion and
        # recursively nested containers. Both can exhaust an inspector's resources.
        depth = 0
        for count, event in enumerate(yaml.parse(body)):
            if isinstance(event, yaml.events.AliasEvent) or count > 100000:
                raise SourceError(422, "invalid_chart_document")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise SourceError(422, "invalid_chart_document")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        # Compose without construction to catch duplicate mapping keys, including
        # JSON schemas, instead of silently taking an attacker's last value.
        node = yaml.compose(body)
        pending = [node]
        while pending:
            current = pending.pop()
            if isinstance(current, yaml.nodes.MappingNode):
                if any(not isinstance(key, yaml.nodes.ScalarNode) for key, _ in current.value):
                    raise SourceError(422, "invalid_chart_document")
                keys = [key.value for key, _ in current.value]
                if len(keys) != len(set(keys)):
                    raise SourceError(422, "invalid_chart_document")
                pending.extend(item for pair in current.value for item in pair)
            elif isinstance(current, yaml.nodes.SequenceNode):
                pending.extend(current.value)
        parsed = json.loads(body) if schema else yaml.safe_load(body)
        if parsed is None and allow_empty:
            return {}
        result = json_value(parsed)
        if not isinstance(result, dict):
            raise SourceError(422, "invalid_chart_document")
        return result
    except (yaml.YAMLError, ValueError, UnicodeError, RecursionError):
        raise SourceError(422, "invalid_chart_document") from None


def inspect_chart(body: bytes, name: str, version: str, digest: str) -> ChartFacts:
    """Check the pin before bounded in-memory inspection; never extract or execute files.

    Nested packaged dependencies are opaque here: they count against archive size
    but are not expanded or executed. Only the selected chart's root configuration
    is returned. Release dependency resolution belongs to a subsequent phase.
    """
    if len(body) > MAX_ARCHIVE_BYTES:
        raise SourceError(422, "chart_archive_too_large")
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise SourceError(422, "source_digest_mismatch")
    try:
        # Cap actual decompression, including padding/trailing members, before tar
        # parsing; declared tar sizes alone do not bound gzip expansion.
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            expanded = compressed.read(MAX_EXPANDED_BYTES + 1)
        if len(expanded) > MAX_EXPANDED_BYTES:
            raise SourceError(422, "chart_expansion_limit")
        documents: dict[str, bytes] = {}
        seen: set[str] = set()
        root = name.rsplit("/", 1)[-1]

        def validate_member(member: tarfile.TarInfo, member_count: int) -> PurePosixPath:
            """Validate one entry before reading it, returning its canonical archive path.

            Paths and member types are checked even for files we do not inspect:
            ignored templates/dependencies must not hide unsafe archive entries.
            Errors identify the failed rule without reflecting upstream filenames.
            """
            if member_count > MAX_FILES:
                raise SourceError(422, "chart_file_limit")
            path = PurePosixPath(member.name)
            if not path.parts:
                raise SourceError(422, "unsafe_chart_path")
            if len(path.parts) > MAX_DEPTH:
                raise SourceError(422, "chart_path_depth_limit")

            # Portable paths cannot escape the chart root or acquire Windows path
            # semantics if a later consumer handles the same verified package.
            if path.is_absolute() or ".." in path.parts:
                raise SourceError(422, "unsafe_chart_path")
            if "\\" in member.name or ":" in member.name:
                raise SourceError(422, "unsafe_chart_path")
            if path.parts[0] != root:
                raise SourceError(422, "unexpected_chart_root")
            if path.as_posix() in seen:
                raise SourceError(422, "duplicate_chart_path")

            # Links, devices and FIFOs have no configuration-inspection meaning.
            # Only ordinary files and directories belong in the verified input.
            if not (member.isfile() or member.isdir()):
                raise SourceError(422, "unsupported_chart_member")
            if member.size < 0 or member.size > MAX_EXPANDED_BYTES:
                raise SourceError(422, "chart_member_size_limit")
            return path

        count = 0
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for member in archive:
                count += 1
                path = validate_member(member, count)
                canonical_name = path.as_posix()
                seen.add(canonical_name)
                if canonical_name in (
                    root + "/Chart.yaml",
                    root + "/values.yaml",
                    root + "/values.schema.json",
                ):
                    if member.size > MAX_DOCUMENT_BYTES:
                        raise SourceError(422, "chart_document_too_large")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise SourceError(422, "invalid_chart_archive")
                    with stream:
                        documents[path.name] = stream.read(MAX_DOCUMENT_BYTES + 1)
        if "Chart.yaml" not in documents:
            raise SourceError(422, "invalid_chart_archive")
        metadata = document(documents["Chart.yaml"])
        if metadata.get("name") != root or metadata.get("version") != version:
            raise SourceError(422, "chart_identity_mismatch")
        defaults = (
            document(documents["values.yaml"], allow_empty=True)
            if "values.yaml" in documents
            else {}
        )
        schema = (
            document(documents["values.schema.json"], schema=True)
            if "values.schema.json" in documents
            else None
        )
        return ChartFacts(name, version, digest, len(body), count, metadata, defaults, schema)
    except (OSError, EOFError, tarfile.TarError, ValueError):
        raise SourceError(422, "invalid_chart_archive") from None
