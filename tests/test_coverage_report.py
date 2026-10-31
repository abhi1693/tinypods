"""Canonical coverage summary inventory, rendering and failure behavior."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/coverage_report.py"


@pytest.fixture
def report_module() -> ModuleType:
    """Load the reporting command without executing its process entrypoint.

    Returns:
        Module exposing the summary and Markdown reporting operations.
    """
    spec = importlib.util.spec_from_file_location("coverage_summary", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def report_root(tmp_path: Path) -> Path:
    """Create a complete four-workspace report with nested installed module paths.

    Args:
        tmp_path: Disposable checkout/configuration/artifact directory.

    Returns:
        Root containing a minimal source inventory, coverage data and JUnit results.
    """
    members = ["apps/api", "apps/cli", "apps/worker", "packages/core"]
    namespaces = ["tinypods_api", "tinypods_cli", "tinypods_worker", "tinypods_core"]
    config = "[tool.uv.workspace]\nmembers=" + json.dumps(members) + "\n"
    config += (
        '[tool.pytest.ini_options]\naddopts="'
        + " ".join("--cov=" + name for name in namespaces)
        + ' --cov-branch"\n'
    )
    config += "[tool.coverage.report]\nfail_under=100\n"
    (tmp_path / "pyproject.toml").write_text(config)
    files = {}
    counters = {"covered_lines": 1, "num_statements": 1, "covered_branches": 1, "num_branches": 1}
    for member, namespace in zip(members, namespaces, strict=True):
        directory = tmp_path / member
        (directory / "src/nested").mkdir(parents=True)
        (directory / "src/nested/module.py").write_text("VALUE = 1\n")
        (directory / "pyproject.toml").write_text(
            '[project]\nname="' + namespace.replace("_", "-") + '"\n'
        )
        files[".venv/lib/python3.13/site-packages/" + namespace + "/nested/module.py"] = {
            "summary": counters.copy()
        }
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/coverage.json").write_text(
        json.dumps(
            {
                "meta": {"branch_coverage": True},
                "files": files,
                "totals": {key: value * 4 for key, value in counters.items()},
            }
        )
    )
    (tmp_path / "reports/junit.xml").write_text(
        '<testsuites><testsuite><testcase name="passed"/></testsuite></testsuites>'
    )
    return tmp_path


def test_summary_matches_complete_inventory_and_reference_layout(
    report_root: Path, report_module: ModuleType
) -> None:
    """Validate nested source mapping and the established visible report structure.

    Args:
        report_root: Complete synthetic checkout and reporting artifacts.
        report_module: Loaded reporting command.
    """
    summary = report_module.summarize(report_root, 0)
    assert summary["status"] == "passed" and summary["source_files"] == 4
    assert summary["tests"] == {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
    text = report_module.markdown(summary)
    assert "minimum combined coverage **100.00%**" in text
    assert "| Workspace | Lines | Branches | Combined |" in text
    assert "| All backend code | 100.00% | 100.00% | 100.00% |" in text


@pytest.mark.parametrize(
    "case",
    [
        "inventory",
        "duplicate",
        "totals",
        "scope",
        "omit",
        "branches",
        "junit",
        "minimum",
        "attempt",
    ],
)
def test_summary_rejects_invalid_artifacts(
    report_root: Path, report_module: ModuleType, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Fail closed when a summary cannot prove complete measurement and valid outcomes.

    Args:
        report_root: Complete artifacts modified only for the selected failure mode.
        report_module: Loaded reporting command.
        monkeypatch: Scoped run-attempt override.
        case: Concrete inventory, scope, metric or artifact validation failure.
    """
    coverage = report_root / "reports/coverage.json"
    document = json.loads(coverage.read_text())
    config = report_root / "pyproject.toml"
    if case == "inventory":
        document["files"].pop(next(iter(document["files"])))
    elif case == "duplicate":
        name, value = next(iter(document["files"].items()))
        document["files"][name.split("site-packages/")[-1]] = value
    elif case == "totals":
        document["totals"]["covered_lines"] -= 1
    elif case == "scope":
        config.write_text(config.read_text().replace("--cov=tinypods_api ", ""))
    elif case == "omit":
        config.write_text(
            config.read_text().replace("fail_under=100", 'fail_under=100\nomit=["something"]')
        )
    elif case == "branches":
        document["meta"]["branch_coverage"] = False
    elif case == "junit":
        (report_root / "reports/junit.xml").write_text("<testsuites/>")
    elif case == "minimum":
        config.write_text(config.read_text().replace("fail_under=100", "fail_under=101"))
    elif case == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "0")
    coverage.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        report_module.summarize(report_root, 0)


def test_summary_command_writes_failure_artifact_when_reports_are_missing(tmp_path: Path) -> None:
    """Produce an explicit invalid marker instead of leaving a stale successful summary.

    Args:
        tmp_path: Empty checkout with deliberately absent configuration and artifacts.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--test-exit-code", "1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    summary = json.loads((tmp_path / "reports/coverage/summary.json").read_text())
    assert summary["valid"] is False and "missing or invalid" in result.stdout


def test_failed_test_status_remains_visible_in_valid_summary(
    report_root: Path, report_module: ModuleType
) -> None:
    """Preserve complete measurements while clearly reporting a failing test process.

    Args:
        report_root: Complete coverage and JUnit inputs.
        report_module: Loaded reporting command.
    """
    summary = report_module.summarize(report_root, 1)
    assert summary["valid"] is True and summary["status"] == "failed"
    assert "**FAILED**" in report_module.markdown(summary)
