"""Validate shared field definitions and values schemas without exposing input values."""

import json

import jsonschema  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT7
from tinypods_core.applications.contracts import ReleaseValidationIssue
from tinypods_core.applications.definitions import ConfigurationField
from tinypods_core.applications.helm_values import set_value, value_at
from tinypods_core.json_types import JsonRecord, JsonValue

MAX_VALIDATION_ISSUES = 20


def same_value(left: JsonValue, right: JsonValue) -> bool:
    """Compare finite JSON defaults without conflating true with one."""
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def field_type_matches(kind: str, value: JsonValue) -> bool:
    """Check widget defaults independently of optional upstream values schemas."""
    if value is None or kind == "choice":
        return True
    if kind in ("text", "password"):
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def configuration_overrides(
    raw_fields: list[JsonRecord], defaults: JsonRecord
) -> tuple[JsonRecord, tuple[ConfigurationField, ...], list[ReleaseValidationIssue]]:
    """Validate user controls and assemble only explicit non-secret defaults."""
    try:
        fields = TypeAdapter(list[ConfigurationField]).validate_python(raw_fields, strict=True)
    except ValidationError:
        return (
            {},
            (),
            [
                ReleaseValidationIssue(
                    code="invalid_configuration_fields", path=["configuration_fields"]
                )
            ],
        )
    overrides: JsonRecord = {}
    issues: list[ReleaseValidationIssue] = []
    paths: set[str] = set()
    for index, field in enumerate(fields):
        location: list[str | int] = ["configuration_fields", index]
        if field.path in paths:
            issues.append(
                ReleaseValidationIssue(code="duplicate_configuration_path", path=location)
            )
        paths.add(field.path)
        exists, chart_default = value_at(defaults, field.path)
        value = field.value if field.value is not None else chart_default
        if not field_type_matches(field.type, value):
            issues.append(ReleaseValidationIssue(code="invalid_field_default", path=location))
        if field.type == "choice":
            choices = field.choices or []
            identities = [json.dumps(item, sort_keys=True) for item in choices]
            if not choices or len(choices) > 100 or len(set(identities)) != len(choices):
                issues.append(ReleaseValidationIssue(code="invalid_field_choices", path=location))
            elif value is not None and not any(same_value(value, choice) for choice in choices):
                issues.append(ReleaseValidationIssue(code="default_not_in_choices", path=location))
        if field.required and field.set_by in ("chart_default", "administrator") and value is None:
            issues.append(ReleaseValidationIssue(code="missing_field_default", path=location))
        if field.set_by == "chart_default":
            if field.value is not None and (
                not exists or not same_value(field.value, chart_default)
            ):
                issues.append(ReleaseValidationIssue(code="chart_default_override", path=location))
        elif field.value is not None:
            set_value(overrides, field.path, field.value)
    ordered = sorted(paths)
    if any(
        right.startswith(left + ".")
        for index, left in enumerate(ordered)
        for right in ordered[index + 1 :]
    ):
        issues.append(
            ReleaseValidationIssue(
                code="overlapping_configuration_paths", path=["configuration_fields"]
            )
        )
    return overrides, tuple(fields), issues[:MAX_VALIDATION_ISSUES]


def validate_values_schema(
    schema: JsonRecord | None, values: JsonRecord, path: tuple[str, ...]
) -> list[ReleaseValidationIssue]:
    """Validate defaults with known drafts and local references only; never retrieve schemas."""
    if schema is None:
        return []
    supported = (
        jsonschema.Draft4Validator,
        jsonschema.Draft6Validator,
        jsonschema.Draft7Validator,
        jsonschema.Draft201909Validator,
        jsonschema.Draft202012Validator,
    )
    declared = schema.get("$schema")
    known = {}
    for validator in supported:
        identifier = str(validator.META_SCHEMA.get("$id", validator.META_SCHEMA.get("id")))
        for alias in (
            identifier,
            identifier.replace("http://", "https://"),
            identifier.replace("https://", "http://"),
        ):
            known[alias] = validator
            known[alias.rstrip("#")] = validator
    if declared is not None and (not isinstance(declared, str) or declared not in known):
        return [ReleaseValidationIssue(code="unsupported_values_schema", path=list(path))]
    try:
        validator_type = (
            known[declared] if isinstance(declared, str) else jsonschema.Draft7Validator
        )
        validator_type.check_schema(schema)
        normalized_schema = dict(schema)
        normalized_schema["$schema"] = str(
            validator_type.META_SCHEMA.get("$id", validator_type.META_SCHEMA.get("id"))
        )
        resource: Resource[JsonValue] = Resource.from_contents(
            normalized_schema, default_specification=DRAFT7
        )
        registry: Registry[JsonValue] = Registry()
        resolver = registry.resolver_with_root(resource)
        pending = [(resource, resolver)]
        while pending:
            current, scope = pending.pop()
            if isinstance(current.contents, dict):
                dialect = current.contents.get("$schema")
                if dialect is not None and (not isinstance(dialect, str) or dialect not in known):
                    return [
                        ReleaseValidationIssue(code="unsupported_values_schema", path=list(path))
                    ]
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    reference = current.contents.get(key)
                    if key in current.contents:
                        if not isinstance(reference, str) or not reference.startswith("#"):
                            return [
                                ReleaseValidationIssue(
                                    code="external_schema_reference", path=list(path)
                                )
                            ]
                        scope.lookup(reference)
            # Specification-aware traversal avoids treating literal default/example
            # objects as schemas, and checks unused subschemas before publication.
            pending.extend((child, scope.in_subresource(child)) for child in current.subresources())
        # The registry has no retrieval callback: resolving a schema cannot read
        # a URL or file, even when its reference is not exercised by defaults.
        validator = validator_type(normalized_schema, registry=registry)
        issues = []
        for error in validator.iter_errors(values):
            location = [
                component for component in error.absolute_path if isinstance(component, (str, int))
            ]
            issues.append(
                ReleaseValidationIssue(code="invalid_chart_defaults", path=[*path, *location])
            )
            if len(issues) == MAX_VALIDATION_ISSUES:
                break
        return issues
    except jsonschema.exceptions.SchemaError:
        return [ReleaseValidationIssue(code="invalid_values_schema", path=list(path))]
    except (Unresolvable, RecursionError):
        return [ReleaseValidationIssue(code="unresolved_schema_reference", path=list(path))]
