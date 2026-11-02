"""Contained Helm graph, defaults, schemas and direct release validation regressions."""

import asyncio

import pytest
from publication_fixtures import chart_archive, draft_for
from tinypods_core.applications.bundles import inspect_bundle, version_matches
from tinypods_core.applications.configuration import configuration_overrides, validate_values_schema
from tinypods_core.applications.helm_values import coalesce, flatten_charts, resolve_values
from tinypods_core.applications.validation import PublicationValidator, validate_definition
from tinypods_core.repositories.sources import SourceError


def test_validated_defaults_without_duplicated_definition() -> None:
    """Validate upstream defaults without creating another stored representation."""
    body = chart_archive(
        schema={"type": "object", "properties": {"replicas": {"type": "integer", "minimum": 1}}}
    )
    result = validate_definition(body, draft_for(body))
    assert not result.issues
    resolved = resolve_values(inspect_bundle(body, draft_for(body).chart), {})
    assert resolved.values == {"replicas": 1}
    assert validate_definition(body, draft_for(body)) == result


@pytest.mark.parametrize(
    "requirement,version,expected",
    [
        ("1.x", "1.8.2", True),
        (">= 1.2 < 2", "2.0.0", False),
        ("^0.2.1", "0.3.0", False),
        ("~1.2.0", "1.2.9", True),
        ("1.0 - 2.0", "2.0.0", True),
        ("<1 || >=2.3.0", "2.4.0", True),
        ("v1.2.0", "v1.2.0", True),
        ("*", "1.2", True),
    ],
)
def test_stable_dependency_ranges(requirement: str, version: str, expected: bool) -> None:
    """Select exact stable packaged versions through normal Helm range declarations."""
    assert version_matches(requirement, version) is expected


@pytest.mark.parametrize(
    "requirement,version,code",
    [
        ("latest", "1.0.0", "mutable_dependency_reference"),
        ("* || latest", "1.0.0", "mutable_dependency_reference"),
        ("*", "1.0.0-rc.1", "prerelease_dependency"),
        ("bad", "1.0.0", "invalid_dependency_version"),
        ("* || bad", "1.0.0", "invalid_dependency_version"),
    ],
)
def test_invalid_or_mutable_ranges(requirement: str, version: str, code: str) -> None:
    """Never hide a mutable/invalid alternate after an earlier matching range."""
    with pytest.raises(SourceError, match=code):
        version_matches(requirement, version)


def test_contained_aliases_conditions_globals_and_imports() -> None:
    """Use one packaged chart for aliases, propagate globals and import child export defaults."""
    child = chart_archive(
        "database",
        "1.2.3",
        values={"enabled": True, "port": 5432, "exports": {"connection": {"database_port": 5432}}},
    )
    body = chart_archive(
        values={
            "global": {"region": "local"},
            "secondary": {"enabled": False},
            "database_port": 9999,
        },
        dependencies=[
            {
                "name": "database",
                "version": "^1.0.0",
                "alias": "primary",
                "import-values": ["connection"],
            },
            {
                "name": "database",
                "version": "1.x",
                "alias": "secondary",
                "condition": "secondary.enabled",
            },
        ],
        children={"database-1.2.3.tgz": child},
    )
    bundled = inspect_bundle(body, draft_for(body).chart)
    resolved = resolve_values(bundled, {})
    nodes = flatten_charts(resolved)
    assert [node.path for node in nodes] == [("root",), ("root", "primary"), ("root", "secondary")]
    assert nodes[1].enabled and not nodes[2].enabled
    assert nodes[1].values["global"] == {"region": "local"}
    assert resolved.values["database_port"] == 9999
    assert len({node.chart.digest for node in nodes[1:]}) == 1
    assert not validate_definition(body, draft_for(body)).issues


def test_unpacked_dependency_and_missing_pin() -> None:
    """Keep unpacked children contained and reject missing dependency pins."""
    body = chart_archive(
        dependencies=[{"name": "database", "version": "1.0.0"}],
        children={
            "database/Chart.yaml": b"apiVersion: v2\nname: database\nversion: 1.0.0\n",
            "database/values.yaml": b"port: 5432",
        },
    )
    result = validate_definition(body, draft_for(body))
    assert not result.issues
    assert inspect_bundle(body, draft_for(body).chart).children[0].kind == "directory"
    missing = chart_archive(dependencies=[{"name": "database", "version": "1.0.0"}])
    assert (
        validate_definition(missing, draft_for(missing)).issues[0].code
        == "unresolved_chart_dependency"
    )


def test_invalid_defaults_references_and_options() -> None:
    """Report safe machine codes without reflecting rejected defaults, values or URLs."""
    body = chart_archive(
        values={"replicas": "sensitive-bad-value"},
        schema={
            "$schema": "https://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"replicas": {"type": "integer"}},
        },
    )
    invalid = validate_definition(body, draft_for(body))
    assert invalid.issues[0].code == "invalid_chart_defaults"
    assert "sensitive-bad-value" not in repr(invalid)
    assert (
        validate_values_schema({"$ref": "https://private.example/schema"}, {}, ("root",))[0].code
        == "external_schema_reference"
    )
    assert (
        validate_definition(body, draft_for(body, options={})).issues[0].code
        == "invalid_architectures"
    )
    assert (
        validate_definition(
            body, draft_for(body, options={"architectures": ["arm64"], "renderer": "untrusted"})
        )
        .issues[0]
        .code
        == "invalid_release_options"
    )
    assert (
        validate_definition(
            body, draft_for(body, options={"architectures": ["arm64"], "core_version_min": "2.0.0"})
        )
        .issues[0]
        .code
        == "incompatible_core_version"
    )


def test_ui_fields_and_merge_semantics() -> None:
    """Reject invalid controls while respecting explicit overrides, map merges and null removal."""
    overrides, fields, issues = configuration_overrides(
        [{"label": "Replicas", "path": "replicas", "type": "number", "set_by": "user", "value": 2}],
        {"replicas": 1},
    )
    assert not issues and overrides == {"replicas": 2} and fields[0].label == "Replicas"
    assert configuration_overrides(
        [{"label": "Bad", "path": "replicas", "type": "number", "value": "bad"}], {"replicas": 1}
    )[2]
    assert coalesce(
        {"parent": {"a": 1, "b": 2}, "array": [1, 2], "remove": 1},
        {"parent": {"a": 9}, "array": [3], "remove": None},
    ) == {"parent": {"a": 9, "b": 2}, "array": [3]}


def test_worker_validation_is_native_async_and_bounded() -> None:
    """Run the real isolated validator without blocking the caller's event loop."""
    body = chart_archive()

    async def scenario() -> None:
        """Complete one process-backed validation under the normal deadline."""
        validator = PublicationValidator()
        result = await validator.validate(body, draft_for(body))
        assert not result.issues
        timed_out = await PublicationValidator(0).validate(body, draft_for(body))
        assert timed_out.issues[0].code == "validation_timeout"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "schema,code",
    [
        ({"$schema": "https://unsupported.example/schema"}, "unsupported_values_schema"),
        ({"type": 9}, "invalid_values_schema"),
        ({"$ref": "#/missing"}, "unresolved_schema_reference"),
        ({"definitions": {"unused": {"$ref": "#/missing"}}}, "unresolved_schema_reference"),
        ({"definitions": {"unused": {"$ref": "file:///private"}}}, "external_schema_reference"),
        ({"$ref": "#"}, "unresolved_schema_reference"),
    ],
)
def test_schema_references_are_local_and_complete(schema: dict, code: str) -> None:
    """Reject broken unused references as well as active network/file references."""
    assert validate_values_schema(schema, {}, ("root",))[0].code == code


def test_local_schema_references_and_literal_data() -> None:
    """Resolve local definitions without interpreting example/default values as schemas."""
    assert not validate_values_schema(
        {
            "definitions": {"count": {"type": "integer"}},
            "properties": {"replicas": {"$ref": "#/definitions/count"}},
            "default": {"$ref": "literal data"},
        },
        {"replicas": 1},
        ("root",),
    )
    schema = {"type": "object", "properties": {str(i): {"type": "integer"} for i in range(30)}}
    assert len(validate_values_schema(schema, {str(i): "bad" for i in range(30)}, ("root",))) == 20


@pytest.mark.parametrize(
    "fields,defaults,code",
    [
        ([{"invalid": True}], {}, "invalid_configuration_fields"),
        (
            [
                {"label": "A", "type": "text", "path": "a"},
                {"label": "Again", "type": "text", "path": "a"},
            ],
            {},
            "duplicate_configuration_path",
        ),
        (
            [{"label": "A", "type": "choice", "path": "a", "choices": []}],
            {},
            "invalid_field_choices",
        ),
        (
            [{"label": "A", "type": "choice", "path": "a", "choices": [1, 1]}],
            {},
            "invalid_field_choices",
        ),
        (
            [{"label": "A", "type": "choice", "path": "a", "choices": [1, 2]}],
            {"a": 3},
            "default_not_in_choices",
        ),
        (
            [{"label": "A", "type": "text", "path": "a", "required": True}],
            {},
            "missing_field_default",
        ),
        (
            [{"label": "A", "type": "text", "path": "a", "value": "override"}],
            {},
            "chart_default_override",
        ),
        (
            [
                {"label": "A", "type": "text", "path": "a"},
                {"label": "Nested", "type": "text", "path": "a.b"},
            ],
            {},
            "overlapping_configuration_paths",
        ),
    ],
)
def test_invalid_ui_field_definitions(fields: list, defaults: dict, code: str) -> None:
    """Validate controls independently of whether the upstream chart provides a schema."""
    assert code in [issue.code for issue in configuration_overrides(fields, defaults)[2]]


def test_valid_widget_defaults_and_paths() -> None:
    """Keep strings, booleans and choices distinct from numeric defaults."""
    from tinypods_core.applications.configuration import field_type_matches
    from tinypods_core.applications.helm_values import set_value, value_at

    assert field_type_matches("text", "value")
    assert field_type_matches("boolean", True)
    assert not field_type_matches("number", True)
    assert not configuration_overrides(
        [{"label": "A", "type": "choice", "path": "a", "choices": [True, 1], "value": True}],
        {"a": True},
    )[2]
    assert not configuration_overrides(
        [{"label": "A", "type": "text", "path": "a", "value": "same"}], {"a": "same"}
    )[2]
    values = {}
    set_value(values, "nested.value", 2)
    assert value_at(values, "nested.value") == (True, 2)
    assert value_at(values, "missing") == (False, None)
    with pytest.raises(SourceError, match="configuration_path_conflict"):
        set_value({"nested": 1}, "nested.value", 2)


@pytest.mark.parametrize(
    "body,code",
    [
        (
            chart_archive(
                dependencies=[{"name": "child", "version": "1"}, {"name": "child", "version": "1"}]
            ),
            "duplicate_dependency_alias",
        ),
        (chart_archive(dependencies=[{"name": "child"}]), "invalid_dependency_definition"),
        (chart_archive(children={"broken.tgz": b"broken"}), "invalid_subchart_archive"),
        (chart_archive(children={"child/data": b"missing metadata"}), "invalid_subchart_metadata"),
        (chart_archive(children={"child/Chart.yaml": b"name: child"}), "invalid_subchart_metadata"),
        (chart_archive(children={"same.tgz": chart_archive()}), "dependency_cycle"),
        (
            chart_archive(
                children={
                    "one.tgz": chart_archive("child", values={"a": 1}),
                    "two.tgz": chart_archive("child", values={"a": 2}),
                }
            ),
            "duplicate_chart_version",
        ),
        (
            chart_archive(
                dependencies=[{"name": "child", "version": "*"}],
                children={"one.tgz": chart_archive("child"), "two.tgz": chart_archive("child")},
            ),
            "ambiguous_chart_dependency",
        ),
    ],
)
def test_contained_graph_integrity(body: bytes, code: str) -> None:
    """Reject cycles, conflicting exact identities and ambiguous or malformed contained charts."""
    assert validate_definition(body, draft_for(body)).issues[0].code == code


def test_bundle_and_validation_work_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound physical archive work and logical alias multiplication."""
    from tinypods_core.applications import bundles, helm_values

    body = chart_archive(children={"child.tgz": chart_archive("child")})
    for constant in ("MAX_BUNDLE_CHARTS", "MAX_BUNDLE_MEMBERS", "MAX_EXPANDED_BYTES"):
        with monkeypatch.context() as patch:
            patch.setattr(bundles, constant, 0)
            assert validate_definition(body, draft_for(body)).issues[0].code == "chart_bundle_limit"
    with monkeypatch.context() as patch:
        patch.setattr(bundles, "MAX_BUNDLE_DEPTH", 0)
        assert validate_definition(body, draft_for(body)).issues[0].code == "dependency_depth_limit"
    for module, constant in (
        (helm_values, "MAX_VALIDATION_BYTES"),
        (helm_values, "MAX_BUNDLE_CHARTS"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(module, constant, 0)
            assert (
                validate_definition(body, draft_for(body)).issues[0].code
                == "release_validation_limit"
            )


def test_declared_compatibility_and_invalid_globals() -> None:
    """Reject malformed bounds and scalar global/dependency values without executing Helm."""
    body = chart_archive()
    assert (
        validate_definition(
            body,
            draft_for(body, options={"architectures": ["arm64"], "core_version_max": "invalid"}),
        )
        .issues[0]
        .code
        == "invalid_core_version"
    )
    for values, code in (
        ({"global": 2}, "invalid_global_values"),
        ({"child": 2}, "invalid_dependency_values"),
    ):
        body = chart_archive(values=values, children={"child.tgz": chart_archive("child")})
        assert validate_definition(body, draft_for(body)).issues[0].code == code


def test_optional_legacy_metadata_and_ignored_chart_files() -> None:
    """Honor legacy dependencies while safely accepting directory markers and ignored files."""
    body = chart_archive(
        children={"child.tgz": chart_archive("child"), ".ignored": b"unused", "README": b"unused"},
        extra={"requirements.yaml": b"dependencies: [{name: child, version: '1.x'}]"},
        directories=("templates",),
    )
    assert not validate_definition(body, draft_for(body)).issues
    assert not configuration_overrides(
        [{"label": "Input", "type": "text", "path": "input", "set_by": "user"}], {}
    )[2]
    assert not validate_values_schema(
        {"properties": {"anything": True}}, {"anything": 1}, ("root",)
    )
    nested = {"properties": {"unused": {"$schema": "https://unknown.example/dialect"}}}
    assert validate_values_schema(nested, {}, ("root",))[0].code == "unsupported_values_schema"
    with pytest.raises(SourceError, match="invalid_dependency_version"):
        version_matches("", "1.0.0")


@pytest.mark.parametrize(
    "child,code",
    [
        (chart_archive(extra={"Chart.yaml": b"name: example"}), "invalid_subchart_metadata"),
        (chart_archive(extra={"Chart.yaml": b"name: 1\nversion: 1"}), "invalid_subchart_metadata"),
        (chart_archive(extra={"Other.yaml": b"data"}), "dependency_cycle"),
    ],
)
def test_nested_claimed_identity(child: bytes, code: str) -> None:
    """Discover nested identities before checking all their pinned archive members."""
    body = chart_archive(children={"child.tgz": child})
    assert validate_definition(body, draft_for(body)).issues[0].code == code


def test_nested_metadata_limits_and_invalid_declarations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject oversized embedded metadata and malformed dependency lists."""
    from tinypods_core.applications import bundles

    child = chart_archive("child")
    with monkeypatch.context() as patch:
        patch.setattr(bundles, "MAX_EXPANDED_BYTES", 1)
        with pytest.raises(SourceError, match="chart_expansion_limit"):
            bundles.package_identity(child)
    import gzip
    import io
    import tarfile

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:") as archive:
        member = tarfile.TarInfo("child/values.yaml")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"{}"))
    with pytest.raises(SourceError, match="invalid_subchart_metadata"):
        bundles.package_identity(gzip.compress(data.getvalue()))
    for metadata in (b"apiVersion: v2\nname: example\nversion: 1.0.0\ndependencies: {}",):
        body = chart_archive(extra={"Chart.yaml": metadata})
        assert (
            validate_definition(body, draft_for(body)).issues[0].code
            == "invalid_dependency_definition"
        )


@pytest.mark.parametrize(
    "imports,code",
    [
        ([{"wrong": "field"}], "invalid_dependency_import"),
        (["missing"], "invalid_dependency_import"),
    ],
)
def test_invalid_dependency_imports(imports: list, code: str) -> None:
    """Fail explicitly when an import cannot resolve a child table."""
    body = chart_archive(
        dependencies=[{"name": "child", "version": "1", "import-values": imports}],
        children={"child.tgz": chart_archive("child")},
    )
    assert validate_definition(body, draft_for(body)).issues[0].code == code


def test_dependency_tags_table_imports_and_duplicate_unmanaged_aliases() -> None:
    """Honor condition precedence over tags and import into a nested parent table."""
    child = chart_archive("child", values={"exports": {"settings": {"port": 80}}})
    body = chart_archive(
        values={"tags": {"web": False}, "not_boolean": "ignored", "enabled": True},
        dependencies=[
            {
                "name": "child",
                "version": "1",
                "tags": ["web"],
                "condition": "not_boolean,enabled",
                "import-values": [{"child": "exports.settings", "parent": "service"}],
            }
        ],
        children={"child.tgz": child},
    )
    resolved = resolve_values(inspect_bundle(body, draft_for(body).chart), {})
    assert resolved.children[0].enabled and resolved.values["service"] == {"port": 80}
    duplicate = chart_archive(children={"one.tgz": child, "two.tgz": child})
    assert (
        validate_definition(duplicate, draft_for(duplicate)).issues[0].code
        == "duplicate_dependency_alias"
    )


def test_child_null_overrides_are_not_resurrected() -> None:
    """Merge a child baseline once even when its values also control enablement."""
    body = chart_archive(
        children={"child.tgz": chart_archive("child", values={"remove": 1, "keep": 2})}
    )
    resolved = resolve_values(
        inspect_bundle(body, draft_for(body).chart), {"child": {"remove": None}}
    )
    assert resolved.children[0].values == {"keep": 2}
    assert resolved.values["child"] == {"keep": 2}


def test_definition_reports_invalid_field_ownership() -> None:
    """A chart-default control cannot publish a differing administrator value."""
    body = chart_archive()
    result = validate_definition(
        body,
        draft_for(
            body, fields=[{"label": "Replicas", "path": "replicas", "type": "number", "value": 2}]
        ),
    )
    assert result.issues[0].code == "chart_default_override"


def test_validation_admission_rejects_before_download_and_releases_on_cancel() -> None:
    """A busy validator does not download or queue another archive; cancellation frees capacity."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tinypods_core.applications.publication import DraftState, ReleasePublicationService

    body = chart_archive()
    downloaded = False

    async def download(pin: object) -> bytes:
        nonlocal downloaded
        downloaded = True
        return body

    async def scenario() -> None:
        validator = PublicationValidator()
        charts = SimpleNamespace(download_archive=download)
        service = ReleasePublicationService(None, None, charts, validator)
        state = DraftState(draft_for(body), datetime.now(UTC))
        async with validator.admission():
            with pytest.raises(SourceError, match="validation_busy") as busy:
                await asyncio.create_task(service.checked(None, state))
            assert busy.value.status == 503 and not downloaded
            with pytest.raises(SourceError, match="validation_busy"):
                await asyncio.create_task(validator.validate(body, state.definition))
        entered = asyncio.Event()

        async def occupied() -> None:
            async with validator.admission():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(occupied())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (await validator.validate(body, state.definition)).issues

    asyncio.run(scenario())


def test_helm_archive_resolution_constructs_yaml_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain digest/URL validation without constructing the repository index twice."""
    import yaml
    from tinypods_core.repositories.verification import archive_url

    original = yaml.safe_load
    calls = 0

    def load(body: bytes) -> object:
        nonlocal calls
        calls += 1
        return original(body)

    monkeypatch.setattr(yaml, "safe_load", load)
    body = (
        b"entries: {example: [{name: example, version: '1.0', digest: '"
        + b"a" * 64
        + b"', urls: [chart.tgz]}]}"
    )
    assert (
        archive_url(body, "example", "1.0", "sha256:" + "a" * 64, "https://example.test", 10)
        == "https://example.test/chart.tgz"
    )
    assert calls == 1
