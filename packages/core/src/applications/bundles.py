"""Inspect exact Helm subcharts contained in a verified parent archive."""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from semantic_version import SimpleSpec, Version  # type: ignore[import-untyped]
from tinypods_core.repositories.archive import (
    MAX_EXPANDED_BYTES,
    ChartFacts,
    document,
    inspect_chart,
)
from tinypods_core.repositories.service import chart_version
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.verification import ChartPin

MAX_BUNDLE_CHARTS = 64
MAX_BUNDLE_DEPTH = 8
MAX_BUNDLE_MEMBERS = 8192


class HelmDependency(BaseModel):
    """Helm dependency declaration; exact resolved identities come from packaged bytes."""

    model_config = ConfigDict(strict=True)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255)
    version: str = Field(min_length=1, max_length=256)
    alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255)
    repository: str = Field(default="", max_length=2048)
    condition: str | None = Field(default=None, max_length=512)
    tags: list[str] | None = Field(default=None, max_length=32)
    imports: list[str | dict[str, str]] | None = Field(
        default=None, alias="import-values", max_length=32
    )

    @property
    def key(self) -> str:
        """Return the values/graph alias without changing the chart's source name."""
        return self.alias or self.name


@dataclass
class BundleBudget:
    """One aggregate work budget across all nested packaged chart archives."""

    expanded_bytes: int = 0
    members: int = 0
    charts: int = 0
    identities: dict[tuple[str, str], str] = field(default_factory=dict)

    def account(self, expanded_bytes: int, members: int) -> None:
        """Reject cumulative expansion/work rather than granting each child a fresh limit."""
        self.expanded_bytes += expanded_bytes
        self.members += members
        self.charts += 1
        if (
            self.expanded_bytes > MAX_EXPANDED_BYTES
            or self.members > MAX_BUNDLE_MEMBERS
            or self.charts > MAX_BUNDLE_CHARTS
        ):
            raise SourceError(422, "chart_bundle_limit")


@dataclass(frozen=True)
class BundledChart:
    """One contained chart and its immutable physical children, before values coalescing."""

    facts: ChartFacts
    digest: str
    kind: str
    source_path: str
    declarations: tuple[HelmDependency, ...]
    children: tuple[BundledChart, ...]


def version_matches(requirement: str, version: str) -> bool:
    """Evaluate stable Helm-style ranges while recording only exact resolved versions."""
    if re.search(r"\blatest\b", requirement, re.IGNORECASE):
        raise SourceError(422, "mutable_dependency_reference")
    try:
        parsed = Version(str(chart_version(version)))
        if parsed.prerelease:
            raise SourceError(422, "prerelease_dependency")
        # Helm permits comma/space conjunctions, ORs and inclusive hyphen ranges.
        # SimpleSpec owns comparator, wildcard, tilde and caret precedence.
        specifications = []
        for alternative in requirement.split("||"):
            clause = re.sub(r"(?<=[<>=!~^])\s+", "", alternative.strip())
            clause = re.sub(r"(^|[<>=!~^,\s])v(?=\d)", r"\1", clause)
            hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", clause)
            if hyphen:
                clause = ">=" + hyphen[1] + ",<=" + hyphen[2]
            else:
                clause = ",".join(part for part in re.split(r"[\s,]+", clause) if part)
            if not clause:
                raise ValueError
            normalized = []
            for comparison in clause.split(","):
                match = re.fullmatch(r"([<>=!~^]*)([0-9xX*]+(?:\.[0-9xX*]+){0,2})(.*)", comparison)
                if match:
                    core = match[2].replace("x", "*").replace("X", "*")
                    comparison = match[1] + core + match[3]
                normalized.append(comparison)
            specifications.append(SimpleSpec(",".join(normalized)))
        return any(specification.match(parsed) for specification in specifications)
    except ValueError:
        raise SourceError(422, "invalid_dependency_version") from None


def contained_files(body: bytes, budget: BundleBudget, members: int) -> dict[str, bytes]:
    """Read ordinary already-validated tar members without extraction or execution."""
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed_stream:
        expanded = compressed_stream.read(MAX_EXPANDED_BYTES + 1)
    budget.account(len(expanded), members)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            # The root verifier checked every member and its declared size before
            # this read. Aggregate expansion remains bounded across recursion.
            with stream:
                files["/".join(PurePosixPath(member.name).parts[1:])] = stream.read(
                    MAX_EXPANDED_BYTES + 1
                )
    return files


def package_directory(files: dict[str, bytes], name: str) -> bytes:
    """Represent an unpacked contained child deterministically for the shared inspector."""
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:") as archive:
        for path, body in sorted(files.items()):
            member = tarfile.TarInfo(name + "/" + path)
            member.size = len(body)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(body))
    return gzip.compress(tar_bytes.getvalue(), mtime=0)


def package_identity(body: bytes) -> tuple[str, str]:
    """Read only root Chart.yaml to discover an embedded archive's claimed identity."""
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed_stream:
        expanded = compressed_stream.read(MAX_EXPANDED_BYTES + 1)
    if len(expanded) > MAX_EXPANDED_BYTES:
        raise SourceError(422, "chart_expansion_limit")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        roots = [
            member
            for member in archive
            if len(PurePosixPath(member.name).parts) == 2
            and PurePosixPath(member.name).name == "Chart.yaml"
            and member.isfile()
        ]
        if len(roots) != 1:
            raise SourceError(422, "invalid_subchart_metadata")
        stream = archive.extractfile(roots[0])
        assert stream is not None
        with stream:
            metadata = document(stream.read(2_000_001))
    name, version = metadata.get("name"), metadata.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SourceError(422, "invalid_subchart_metadata")
    return name, version


def dependency_declarations(
    facts: ChartFacts, files: dict[str, bytes]
) -> tuple[HelmDependency, ...]:
    """Support modern Chart.yaml and legacy requirements.yaml without external resolution."""
    raw = facts.metadata.get("dependencies")
    if raw is None and "requirements.yaml" in files:
        raw = document(files["requirements.yaml"]).get("dependencies")
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_BUNDLE_CHARTS:
        raise SourceError(422, "invalid_dependency_definition")
    try:
        declarations = tuple(HelmDependency.model_validate(item) for item in raw)
    except ValidationError:
        raise SourceError(422, "invalid_dependency_definition") from None
    if len({item.key for item in declarations}) != len(declarations):
        raise SourceError(422, "duplicate_dependency_alias")
    return declarations


def inspect_bundle(body: bytes, pin: ChartPin) -> BundledChart:
    """Verify the parent pin and every contained chart with shared graph/resource bounds."""
    budget = BundleBudget()

    def visit(
        archive_bytes: bytes,
        name: str,
        version: str,
        digest: str,
        source_path: str,
        ancestors: tuple[tuple[str, str], ...],
        kind: str = "archive",
    ) -> BundledChart:
        """Inspect one node before traversing its physical packaged/directory children."""
        identity = (name.rsplit("/", 1)[-1], version)
        if identity in ancestors:
            raise SourceError(422, "dependency_cycle")
        if len(ancestors) > MAX_BUNDLE_DEPTH:
            raise SourceError(422, "dependency_depth_limit")
        facts = inspect_chart(archive_bytes, name, version, digest)
        version_matches("*", version)
        previous = budget.identities.setdefault(identity, digest)
        if previous != digest:
            raise SourceError(422, "duplicate_chart_version")
        files = contained_files(archive_bytes, budget, facts.files)
        declarations = dependency_declarations(facts, files)
        children: list[BundledChart] = []
        directories: dict[str, dict[str, bytes]] = {}
        for path, child_bytes in sorted(files.items()):
            parts = PurePosixPath(path).parts
            if len(parts) < 2 or parts[0] != "charts" or parts[1].startswith((".", "_")):
                continue
            if len(parts) == 2 and path.endswith(".tgz"):
                child_name, child_version = package_identity(child_bytes)
                child_digest = "sha256:" + hashlib.sha256(child_bytes).hexdigest()
                children.append(
                    visit(
                        child_bytes,
                        child_name,
                        child_version,
                        child_digest,
                        source_path + "/" + path,
                        ancestors + (identity,),
                    )
                )
            elif len(parts) > 2:
                directories.setdefault(parts[1], {})["/".join(parts[2:])] = child_bytes
        for folder, child_files in sorted(directories.items()):
            if "Chart.yaml" not in child_files:
                raise SourceError(422, "invalid_subchart_metadata")
            metadata = document(child_files["Chart.yaml"])
            claimed_name, claimed_version = metadata.get("name"), metadata.get("version")
            if not isinstance(claimed_name, str) or not isinstance(claimed_version, str):
                raise SourceError(422, "invalid_subchart_metadata")
            child_bytes = package_directory(child_files, claimed_name)
            child_digest = "sha256:" + hashlib.sha256(child_bytes).hexdigest()
            children.append(
                visit(
                    child_bytes,
                    claimed_name,
                    claimed_version,
                    child_digest,
                    source_path + "/charts/" + folder,
                    ancestors + (identity,),
                    "directory",
                )
            )
        return BundledChart(facts, digest, kind, source_path, declarations, tuple(children))

    try:
        return visit(body, pin.name, pin.version, pin.digest, ".", ())
    except (OSError, EOFError, tarfile.TarError):
        raise SourceError(422, "invalid_subchart_archive") from None
