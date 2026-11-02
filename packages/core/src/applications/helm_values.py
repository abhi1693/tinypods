"""Coalesce contained Helm dependency values without executing chart templates."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

from tinypods_core.applications.bundles import (
    MAX_BUNDLE_CHARTS,
    BundledChart,
    HelmDependency,
    version_matches,
)
from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.repositories.sources import SourceError

MAX_VALIDATION_BYTES = 8_000_000


def coalesce(defaults: JsonRecord, overrides: JsonRecord) -> JsonRecord:
    """Merge maps, replace arrays/scalars and remove explicit null overrides as Helm does."""
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        existing = result.get(key)
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(existing, dict):
            result[key] = coalesce(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def value_at(values: JsonRecord, path: str) -> tuple[bool, JsonValue]:
    """Resolve a dot-separated Helm condition/configuration path without inventing keys."""
    current: JsonValue = values
    for component in path.split("."):
        if not isinstance(current, dict) or component not in current:
            return False, None
        current = current[component]
    return True, current


def set_value(values: JsonRecord, path: str, value: JsonValue) -> None:
    """Set one declared configuration path, rejecting traversal through scalar/array values."""
    current = values
    components = path.split(".")
    for component in components[:-1]:
        nested = current.setdefault(component, {})
        if not isinstance(nested, dict):
            raise SourceError(422, "configuration_path_conflict")
        current = nested
    current[components[-1]] = copy.deepcopy(value)


def dependency_enabled(declaration: HelmDependency, values: JsonRecord) -> bool:
    """Apply Helm's tag rules, then its first existing boolean condition override."""
    tags = values.get("tags")
    tag_values = (
        [tags.get(tag) for tag in (declaration.tags or [])] if isinstance(tags, dict) else []
    )
    enabled = any(item is True for item in tag_values) or not any(
        item is False for item in tag_values
    )
    for condition in (declaration.condition or "").strip().split(","):
        if condition:
            exists, value = value_at(values, condition)
            if exists and isinstance(value, bool):
                return value
    return enabled


@dataclass(frozen=True)
class ResolvedChart:
    """A logical dependency alias with coalesced baseline values and exact physical facts."""

    chart: BundledChart
    path: tuple[str, ...]
    values: JsonRecord
    enabled: bool
    children: tuple[ResolvedChart, ...]


@dataclass
class ResolutionBudget:
    """Limit logical alias expansion as well as physical archive expansion."""

    charts: int = 0
    document_bytes: int = 0

    def account(self, chart: BundledChart) -> None:
        """Reject alias multiplication before copying large configuration subtrees."""
        self.charts += 1
        self.document_bytes += len(
            json.dumps(
                {
                    "metadata": chart.facts.metadata,
                    "defaults": chart.facts.defaults,
                    "schema": chart.facts.values_schema,
                },
                ensure_ascii=False,
            ).encode()
        )
        if self.charts > MAX_BUNDLE_CHARTS or self.document_bytes > MAX_VALIDATION_BYTES:
            raise SourceError(422, "release_validation_limit")


def select_dependencies(
    chart: BundledChart,
) -> tuple[tuple[str, BundledChart, HelmDependency | None], ...]:
    """Resolve declarations to contained exact versions, retaining unmanaged packaged children."""
    selected: list[tuple[str, BundledChart, HelmDependency | None]] = []
    used: set[str] = set()
    for declaration in chart.declarations:
        candidates = [
            child
            for child in chart.children
            if child.facts.metadata["name"] in (declaration.name, declaration.alias)
            and version_matches(declaration.version, child.facts.version)
        ]
        if len(candidates) != 1:
            raise SourceError(
                422,
                "unresolved_chart_dependency" if not candidates else "ambiguous_chart_dependency",
            )
        selected.append((declaration.key, candidates[0], declaration))
        used.add(candidates[0].source_path)
    for child in chart.children:
        if child.source_path not in used:
            selected.append((str(child.facts.metadata["name"]), child, None))
    if len({alias for alias, _, _ in selected}) != len(selected):
        raise SourceError(422, "duplicate_dependency_alias")
    return tuple(sorted(selected, key=lambda item: item[0]))


def import_values(
    values: JsonRecord, imports: list[str | dict[str, str]] | None, child: ResolvedChart
) -> JsonRecord:
    """Import child exports/tables as lower-priority defaults, preserving parent choices."""
    imported: JsonRecord = {}
    for entry in imports or []:
        if isinstance(entry, str):
            child_path, parent_path = "exports." + entry, ""
        elif set(entry) == {"child", "parent"}:
            child_path, parent_path = entry["child"], entry["parent"]
        else:
            raise SourceError(422, "invalid_dependency_import")
        exists, value = value_at(child.values, child_path)
        if not exists or not isinstance(value, dict):
            raise SourceError(422, "invalid_dependency_import")
        if parent_path in ("", "."):
            imported = coalesce(imported, value)
        else:
            set_value(imported, parent_path, value)
    return coalesce(imported, values)


def resolve_values(chart: BundledChart, overrides: JsonRecord) -> ResolvedChart:
    """Resolve exact contained dependencies and baseline values without external downloads."""
    budget = ResolutionBudget()

    def resolve(
        current: BundledChart,
        supplied: JsonRecord,
        path: tuple[str, ...],
        globals_: JsonRecord,
        enabled: bool,
        coalesced: bool = False,
    ) -> ResolvedChart:
        """Resolve one alias tree while bounding repeated shared physical chart inputs."""
        budget.account(current)
        # Child defaults were merged for condition evaluation already. Repeating
        # that merge would resurrect keys explicitly removed with null.
        values = (
            copy.deepcopy(supplied) if coalesced else coalesce(current.facts.defaults, supplied)
        )
        own_globals = values.get("global", {})
        if not isinstance(own_globals, dict):
            raise SourceError(422, "invalid_global_values")
        if globals_ or "global" in values:
            values["global"] = coalesce(own_globals, globals_)
        inherited = values.get("global", {})
        assert isinstance(inherited, dict)
        children: list[ResolvedChart] = []
        declarations = select_dependencies(current)
        # Coalesce child defaults before interpreting condition paths, matching
        # Helm conditions such as child.enabled even when the parent omits them.
        for alias, child, _ in declarations:
            provided = values.get(alias, {})
            if not isinstance(provided, dict):
                raise SourceError(422, "invalid_dependency_values")
            values[alias] = coalesce(child.facts.defaults, provided)
        for alias, child, declaration in declarations:
            supplied_child = values[alias]
            assert isinstance(supplied_child, dict)
            active = enabled and (declaration is None or dependency_enabled(declaration, values))
            resolved = resolve(child, supplied_child, path + (alias,), inherited, active, True)
            children.append(resolved)
            values[alias] = resolved.values
            if active and declaration is not None:
                values = import_values(values, declaration.imports, resolved)
        return ResolvedChart(current, path, values, enabled, tuple(children))

    return resolve(chart, overrides, ("root",), {}, True)


def flatten_charts(root: ResolvedChart) -> tuple[ResolvedChart, ...]:
    """Return deterministic parent-before-child graph order without duplicating definitions."""
    result = [root]
    for child in root.children:
        result.extend(flatten_charts(child))
    return tuple(result)
