"""Deterministic contained Helm fixtures for release publication tests."""

import gzip
import hashlib
import io
import json
import tarfile
from uuid import UUID

import yaml  # type: ignore[import-untyped]
from tinypods_core.applications.validation import ReleaseDraft
from tinypods_core.json_types import JsonRecord
from tinypods_core.repositories.verification import ChartPin


def chart_archive(
    name: str = "example",
    version: str = "1.0.0",
    *,
    values: JsonRecord | None = None,
    schema: JsonRecord | None = None,
    dependencies: list[JsonRecord] | None = None,
    children: dict[str, bytes] | None = None,
    extra: dict[str, bytes] | None = None,
    directories: tuple[str, ...] = (),
) -> bytes:
    """Package real tar/gzip bytes with no network or filesystem extraction."""
    metadata: JsonRecord = {"apiVersion": "v2", "name": name, "version": version}
    if dependencies is not None:
        metadata["dependencies"] = [item for item in dependencies]
    files = {
        "Chart.yaml": yaml.safe_dump(metadata).encode(),
        "values.yaml": yaml.safe_dump(values if values is not None else {"replicas": 1}).encode(),
    }
    if schema is not None:
        files["values.schema.json"] = json.dumps(schema).encode()
    files.update({"charts/" + path: body for path, body in (children or {}).items()})
    files.update(extra or {})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for path in directories:
            entry = tarfile.TarInfo(name + "/" + path + "/")
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
        for path, body in sorted(files.items()):
            entry = tarfile.TarInfo(name + "/" + path)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    return gzip.compress(output.getvalue(), mtime=0)


def draft_for(
    body: bytes,
    *,
    name: str = "example",
    version: str = "1.0.0",
    fields: list[JsonRecord] | None = None,
    options: JsonRecord | None = None,
) -> ReleaseDraft:
    """Build a detached exact pin and a stable revision identity."""
    pin = ChartPin(
        "helm_repository",
        "https://charts.example.test",
        name,
        version,
        "sha256:" + hashlib.sha256(body).hexdigest(),
    )
    return ReleaseDraft(
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        1,
        fields or [],
        {"architectures": ["arm64"]} if options is None else options,
        "0.0.1",
        pin,
    )
