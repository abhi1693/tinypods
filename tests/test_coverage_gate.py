"""Check exact coverage enforcement, including rounded and branch-disabled reports."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_coverage.py"


def complete_report() -> dict[str, Any]:
    """Build a minimal complete coverage artifact with both execution dimensions.

    Returns:
        Coverage.py-shaped report with one covered branch and no missing code.
    """
    return {
        "meta": {"branch_coverage": True},
        "totals": {
            "num_statements": 2,
            "covered_lines": 2,
            "num_branches": 1,
            "covered_branches": 1,
            "missing_lines": 0,
            "missing_branches": 0,
        },
        "files": {"source.py": {"missing_lines": [], "missing_branches": []}},
    }


def run_gate(tmp_path: Path, content: str | None) -> subprocess.CompletedProcess[str]:
    """Invoke the actual CI entrypoint with an isolated report file.

    Args:
        tmp_path: Directory receiving only the synthetic test artifact.
        content: Report content, or None to exercise a missing artifact.

    Returns:
        Completed process with captured status and diagnostics.
    """
    path = tmp_path / "coverage.json"
    if content is not None:
        path.write_text(content)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, timeout=10
    )


def test_complete_statement_and_branch_counts_pass(tmp_path: Path) -> None:
    """Accept an artifact only when its complete counts and per-file data agree.

    Args:
        tmp_path: Directory receiving the complete test artifact.
    """
    result = run_gate(tmp_path, json.dumps(complete_report()))
    assert result.returncode == 0 and "coverage confirmed" in result.stdout


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "json",
        "shape",
        "disabled",
        "files",
        "empty",
        "counts",
        "line",
        "branch",
        "rounded",
        "file_shape",
        "file_miss",
        "file_missing_key",
    ],
)
def test_incomplete_or_invalid_coverage_fails(tmp_path: Path, case: str) -> None:
    """Reject misses and malformed artifacts without trusting displayed percentages.

    Args:
        tmp_path: Directory receiving an intentionally invalid artifact.
        case: Concrete failure mode that must produce a nonzero CI process status.
    """
    report = complete_report()
    content: str | None
    if case == "missing":
        content = None
    elif case == "json":
        content = "not JSON"
    elif case == "shape":
        content = "[]"
    else:
        if case == "disabled":
            report["meta"]["branch_coverage"] = False
        elif case == "files":
            report["files"] = {}
        elif case == "empty":
            report["totals"]["num_statements"] = 0
        elif case == "counts":
            report["totals"]["covered_lines"] = True
        elif case == "line":
            report["totals"].update(covered_lines=1, missing_lines=1)
        elif case == "branch":
            report["totals"].update(covered_branches=0, missing_branches=1)
        elif case == "rounded":
            report["totals"].update(
                num_statements=1000000, covered_lines=999999, missing_lines=1, percent_covered=100.0
            )
        elif case == "file_shape":
            report["files"]["source.py"] = None
        elif case == "file_miss":
            report["files"]["source.py"]["missing_branches"] = [[1, -1]]
        elif case == "file_missing_key":
            del report["files"]["source.py"]["missing_lines"]
        content = json.dumps(report)
    result = run_gate(tmp_path, content)
    assert result.returncode == 1 and "Coverage check failed" in result.stderr
