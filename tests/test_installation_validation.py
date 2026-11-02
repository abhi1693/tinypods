"""Bounded configuration policy and confidential propagation regressions."""

import asyncio
from dataclasses import replace

import anyio
import pytest
from publication_fixtures import chart_archive, draft_for
from tinypods_core.applications.installation_validation import (
    InstallationValidator,
    remove_value,
    validate_installation,
)
from tinypods_core.json_types import JsonRecord


def field(path: str, **options: object) -> JsonRecord:
    """Describe a controlled configuration field without release-shared secrets."""
    return {"label": path, "path": path, "type": "text", "set_by": "user", **options}  # type: ignore[dict-item]


@pytest.mark.parametrize(
    ("fields", "overrides", "secret", "references", "values", "code"),
    [
        ([field("nested.name")], {"nested": {"name": "chosen"}}, {}, {}, {}, None),
        (
            [field("object", type="choice", choices=[{"a": 1}])],
            {"object": {"a": 1}},
            {},
            {},
            {},
            None,
        ),
        ([field("number", type="number")], {"number": "bad"}, {}, {}, {}, "invalid_field_value"),
        (
            [field("option", type="choice", choices=["a", "b"])],
            {"option": "c"},
            {},
            {},
            {},
            "invalid_field_value",
        ),
        (
            [field("fixed", set_by="administrator", value="fixed")],
            {"fixed": "override"},
            {},
            {},
            {},
            "field_not_writable",
        ),
        ([field("required", required=True)], {}, {}, {}, {}, "required_configuration"),
        (
            [field("generated", set_by="auto", required=True)],
            {},
            {},
            {},
            {},
            "automatic_value_unavailable",
        ),
        ([field("generated", set_by="auto")], {}, {}, {}, {}, None),
        (
            [field("password", type="password")],
            {"password": "literal"},
            {},
            {},
            {},
            "confidential_literal",
        ),
        (
            [field("password", type="password")],
            {},
            {},
            {},
            {"password": "chart-confidential"},
            "confidential_default",
        ),
        ([field("password", type="password")], {}, {}, {}, {"password": ""}, None),
        ([field("password", type="password")], {}, {"password": "fixture-only"}, {}, {}, None),
        ([field("name")], {}, {"name": "fixture-only"}, {}, {}, "invalid_secret_target"),
        ([], {}, {"unknown": "fixture-only"}, {}, {}, "invalid_secret_target"),
        ([field("name")], {}, {}, {"name": "existing"}, {}, None),
        (
            [field("password", type="password")],
            {},
            {},
            {"password": "existing"},
            {},
            "invalid_kubernetes_secret_target",
        ),
        ([], {}, {}, {"unknown": "existing"}, {}, "invalid_kubernetes_secret_target"),
        ([field("parent.child")], {"parent": "wrong"}, {}, {}, {}, "unknown_configuration_path"),
        ([field("duplicate"), field("duplicate")], {}, {}, {}, {}, "duplicate_configuration_path"),
        (
            [field("global.password", type="password")],
            {},
            {"global.password": "fixture-only"},
            {},
            {"global": {}, "echo": "fixture-only"},
            "confidential_value_propagation",
        ),
    ],
)
def test_configuration_policy(
    fields: list[JsonRecord],
    overrides: JsonRecord,
    secret: dict[str, str],
    references: dict[str, str],
    values: JsonRecord,
    code: str | None,
) -> None:
    """Validate declared choices and reject confidential or unsupported configuration."""
    body = chart_archive(values=values)
    result = validate_installation(
        body, draft_for(body, fields=fields), overrides, secret, references
    )
    assert ([issue.code for issue in result.issues][0] if result.issues else None) == code
    if code:
        assert result.values == {}
    assert "fixture-only" not in repr(result)
    if "password" in secret:
        assert "password" not in result.values


def test_configuration_bounds_and_schema() -> None:
    """Bound malformed/large configuration and preserve safe schema locations."""
    body = chart_archive(
        values={"number": 1}, schema={"type": "object", "properties": {"number": {"maximum": 2}}}
    )
    draft = draft_for(body, fields=[field("number", type="number")])
    result = validate_installation(body, draft, {"number": 3}, {}, {})
    assert result.issues[0].code == "invalid_chart_defaults"
    assert validate_installation(b"bad", draft, {}, {}, {}).issues
    assert (
        validate_installation(body, replace(draft, options={"architectures": []}), {}, {}, {})
        .issues[0]
        .code
        == "invalid_architectures"
    )
    assert (
        validate_installation(body, draft, {"huge": "x" * 8_000_001}, {}, {}).issues[0].code
        == "configuration_limit"
    )
    assert (
        validate_installation(body, draft, {"nan": float("nan")}, {}, {}).issues[0].code
        == "invalid_configuration"
    )
    values: JsonRecord = {"parent": "scalar"}
    remove_value(values, "parent.child")
    assert values == {"parent": "scalar"}
    values = {"parent": {"child": "removed", "keep": 1}}
    remove_value(values, "parent.child")
    assert values == {"parent": {"keep": 1}}
    fields = [field("required", required=True)]
    assert validate_installation(
        body, draft_for(body, fields=fields), {"required": None}, {}, {}
    ).issues


def test_validator_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Translate bounded worker timeout while preserving cancellation behavior."""
    from tinypods_core.applications import installation_validation

    async def blocked(*args: object, **kwargs: object) -> None:
        await anyio.sleep_forever()

    async def scenario() -> None:
        body = chart_archive()
        validator = InstallationValidator(timeout_seconds=0.01)
        monkeypatch.setattr(installation_validation.to_process, "run_sync", blocked)
        async with validator.admission():
            result = await validator.configuration(body, draft_for(body), {}, {}, {})
        assert result.issues[0].code == "validation_timeout"

    asyncio.run(scenario())


def test_nested_helm_confidential_propagation_and_disabled_schema() -> None:
    """Real packaged globals/imports cannot leak secrets; disabled dependencies skip schemas."""
    child = chart_archive(name="child", values={}, schema={"required": ["absent"]})
    parent = chart_archive(
        values={"global": {}, "child": {"enabled": False}},
        dependencies=[{"name": "child", "version": "1.0.0", "condition": "child.enabled"}],
        children={"child-1.0.0.tgz": child},
    )
    assert not validate_installation(parent, draft_for(parent), {}, {}, {}).issues
    result = validate_installation(
        parent,
        draft_for(parent, fields=[field("global.password", type="password")]),
        {},
        {"global.password": "fixture-nested-secret"},
        {},
    )
    assert result.issues[0].code == "confidential_value_propagation"
    exported = chart_archive(name="child", values={"exports": {"credentials": {"password": ""}}})
    imported = chart_archive(
        values={},
        dependencies=[{"name": "child", "version": "1.0.0", "import-values": ["credentials"]}],
        children={"child-1.0.0.tgz": exported},
    )
    result = validate_installation(
        imported,
        draft_for(imported, fields=[field("child.exports.credentials.password", type="password")]),
        {},
        {"child.exports.credentials.password": "fixture-imported-secret"},
        {},
    )
    assert result.issues[0].code == "confidential_value_propagation"


def test_bounded_issue_collection_and_confidential_arrays() -> None:
    """Bound issue allocation during traversal and scan array scalars without recursion."""
    from tinypods_core.applications.installation_validation import contains_confidential

    body = chart_archive()
    result = validate_installation(body, draft_for(body), {str(i): i for i in range(50)}, {}, {})
    assert len(result.issues) == 20
    assert contains_confidential(
        {"values": ["safe", {"key": "fixture-secret"}]}, {"target": "fixture-secret"}
    )
    assert not contains_confidential(
        {"values": ["safe", 1, None, False]}, {"target": "fixture-secret"}
    )


@pytest.mark.parametrize("name", ["a..b", "a.-b", "a-.b", ".a", "a."])
def test_kubernetes_reference_name_has_valid_labels(name: str) -> None:
    """Structural checks reject empty DNS labels and label-edge hyphens."""
    from pydantic import ValidationError
    from tinypods_core.applications.installation_contracts import KubernetesSecretBinding

    with pytest.raises(ValidationError):
        KubernetesSecretBinding(
            namespace="test", name=name, key="password", target="existingSecret"
        )
