"""Bounded configuration resolution with explicit confidential-field handling."""

import json
from dataclasses import dataclass, field

import anyio
from anyio import to_process
from tinypods_core.applications.bundles import inspect_bundle
from tinypods_core.applications.configuration import (
    configuration_overrides,
    field_type_matches,
    same_value,
    validate_values_schema,
)
from tinypods_core.applications.contracts import ReleaseOptions, ReleaseValidationIssue
from tinypods_core.applications.definitions import ConfigurationField
from tinypods_core.applications.helm_values import (
    MAX_VALIDATION_BYTES,
    coalesce,
    flatten_charts,
    resolve_values,
    set_value,
    value_at,
)
from tinypods_core.applications.validation import (
    PublicationValidator,
    ReleaseDraft,
    compatibility_issues,
)
from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.repositories.sources import SourceError


@dataclass(frozen=True)
class InstallationConfiguration:
    """Carry safe resolved settings and bounded issue locations."""

    values: JsonRecord = field(default_factory=dict, repr=False)
    issues: tuple[ReleaseValidationIssue, ...] = ()


def remove_value(values: JsonRecord, path: str) -> None:
    """Remove a confidential leaf before returning a configuration snapshot."""
    current = values
    components = path.split(".")
    for component in components[:-1]:
        nested = current.get(component)
        if not isinstance(nested, dict):
            return
        current = nested
    current.pop(components[-1], None)


def confidential_field(item: ConfigurationField) -> bool:
    """Use explicit catalog classification, never path suffixes."""
    return item.sensitive or item.type == "password"


def user_value_issue(item: ConfigurationField, value: JsonValue) -> str | None:
    """Classify a declared user input without copying or exposing its value."""
    if confidential_field(item):
        return "confidential_literal"
    if item.set_by != "user":
        return "field_not_writable"
    if not field_type_matches(item.type, value) or (
        item.type == "choice"
        and not any(same_value(value, choice) for choice in item.choices or [])
    ):
        return "invalid_field_value"
    return None


def user_overrides(
    allowed: dict[str, ConfigurationField],
    overrides: JsonRecord,
) -> tuple[JsonRecord, list[ReleaseValidationIssue]]:
    """Traverse supplied choices once using precomputed declared path prefixes."""
    prefixes: set[str] = set()
    for path in allowed:
        parts = path.split(".")
        prefixes.update(".".join(parts[:index]) for index in range(1, len(parts)))
    supplied: JsonRecord = {}
    issues: list[ReleaseValidationIssue] = []
    pending = [(overrides, "")]
    while pending and len(issues) < 20:
        node, prefix = pending.pop()
        for key, value in node.items():
            if len(issues) == 20:
                break
            path = prefix + key
            item = allowed.get(path)
            if item is not None:
                code = user_value_issue(item, value)
                if code is None:
                    set_value(supplied, path, value)
                else:
                    issues.append(ReleaseValidationIssue(code=code, path=["overrides", path]))
            elif isinstance(value, dict) and path in prefixes:
                pending.append((value, path + "."))
            else:
                issues.append(
                    ReleaseValidationIssue(
                        code="unknown_configuration_path",
                        path=["overrides", path],
                    )
                )
    return supplied, issues


def contains_confidential(values: JsonRecord, confidential: dict[str, str]) -> bool:
    """Scan resolved scalars once for confidential data propagated by Helm imports/globals."""
    pending: list[JsonValue] = [values]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if any(secret in value for secret in confidential.values()):
                return True
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def validate_installation(
    body: bytes,
    draft: ReleaseDraft,
    overrides: JsonRecord,
    confidential: dict[str, str],
    references: dict[str, str],
) -> InstallationConfiguration:
    """Resolve supported user fields; confidential bytes exist only in this worker.

    Unknown paths and confidential literals fail closed. Schema validation sees
    transient decrypted values, but successful results remove every declared
    confidential target. No rejected values are returned on failures.
    """
    try:
        if len(json.dumps(overrides, allow_nan=False).encode()) > MAX_VALIDATION_BYTES:
            raise SourceError(422, "configuration_limit")
        bundle = inspect_bundle(body, draft.chart)
        baseline, fields, issues = configuration_overrides(
            draft.configuration_fields, bundle.facts.defaults
        )
        issues.extend(
            compatibility_issues(ReleaseOptions.model_validate(draft.options), draft.core_version)
        )
        allowed = {item.path: item for item in fields}
        supplied, user_issues = user_overrides(allowed, overrides)
        issues.extend(user_issues)
        for path in confidential:
            item = allowed.get(path)
            if item is None or not confidential_field(item) or item.set_by != "user":
                issues.append(
                    ReleaseValidationIssue(code="invalid_secret_target", path=["secrets", path])
                )
            else:
                set_value(supplied, path, confidential[path])
        for path, name in references.items():
            item = allowed.get(path)
            if item is None or confidential_field(item) or item.set_by != "user":
                issues.append(
                    ReleaseValidationIssue(
                        code="invalid_kubernetes_secret_target", path=["kubernetes_secrets", path]
                    )
                )
            else:
                set_value(supplied, path, name)
        if issues:
            return InstallationConfiguration(issues=tuple(issues[:20]))
        resolved = resolve_values(bundle, coalesce(baseline, supplied))
        for node in flatten_charts(resolved):
            if node.enabled:
                issues.extend(
                    validate_values_schema(node.chart.facts.values_schema, node.values, node.path)
                )
        # Schema validation needs complete transient values first. This worker owns
        # the resolved tree, so checking requirements and stripping secrets share
        # one field pass without a second deep copy.
        values = resolved.values
        for item in fields:
            exists, value = value_at(resolved.values, item.path)
            if item.set_by == "auto":
                # Automatic controls have no producer in this no-deployment scope.
                if item.required:
                    issues.append(
                        ReleaseValidationIssue(code="automatic_value_unavailable", path=[item.path])
                    )
            elif item.required and (not exists or value is None):
                issues.append(
                    ReleaseValidationIssue(code="required_configuration", path=[item.path])
                )
            if (
                confidential_field(item)
                and exists
                and value not in (None, "")
                and item.path not in confidential
            ):
                issues.append(ReleaseValidationIssue(code="confidential_default", path=[item.path]))
            if confidential_field(item):
                remove_value(values, item.path)

        if confidential and contains_confidential(values, confidential):
            issues.append(ReleaseValidationIssue(code="confidential_value_propagation"))
        return InstallationConfiguration(values if not issues else {}, tuple(issues[:20]))
    except SourceError as error:
        # Archive/value helpers own these safe codes; raw upstream errors and
        # confidential values never become validation messages.
        return InstallationConfiguration(issues=(ReleaseValidationIssue(code=error.code),))
    except (ValueError, RecursionError):
        # Malformed JSON/options and excessive nesting are untrusted inputs.
        # Other exceptions propagate as bugs instead of being labelled invalid.
        return InstallationConfiguration(
            issues=(ReleaseValidationIssue(code="invalid_configuration"),)
        )


class InstallationValidator(PublicationValidator):
    """Reuse admission and cancellable worker deadlines for installation values."""

    async def configuration(
        self,
        body: bytes,
        draft: ReleaseDraft,
        overrides: JsonRecord,
        confidential: dict[str, str],
        references: dict[str, str],
    ) -> InstallationConfiguration:
        """Run inside an existing admission reservation without retaining queued archives."""
        try:
            with anyio.fail_after(self.timeout_seconds):
                return await to_process.run_sync(
                    validate_installation,
                    body,
                    draft,
                    overrides,
                    confidential,
                    references,
                    cancellable=True,
                    limiter=anyio.CapacityLimiter(1),
                )
        except TimeoutError:
            # A schema may contain pathological regexes/recursion. The process
            # deadline terminates that work; a worker thread cannot enforce it.
            return InstallationConfiguration(
                issues=(ReleaseValidationIssue(code="validation_timeout"),)
            )
