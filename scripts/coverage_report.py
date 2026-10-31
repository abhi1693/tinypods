"""Validate whole-backend coverage and produce the canonical PR reporting artifact."""

import argparse
import json
import math
import os
import shlex
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

type CounterName = Literal["covered_lines", "num_statements", "covered_branches", "num_branches"]

COUNTERS: Final[tuple[CounterName, ...]] = (
    "covered_lines",
    "num_statements",
    "covered_branches",
    "num_branches",
)


class Counts(TypedDict):
    """Carry exact measured and executed statement/branch counts at the report boundary."""

    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int


class Metrics(Counts):
    """Add display percentages without replacing the underlying exact counts."""

    line_percent: float
    branch_percent: float
    combined_percent: float


class TestCounts(TypedDict):
    """Describe complete JUnit outcomes without including test names or failure contents."""

    total: int
    failed: int
    skipped: int
    passed: int


class Summary(TypedDict):
    """Represent a validated reporting artifact for the data-only comment job."""

    schema: int
    valid: Literal[True]
    status: Literal["passed", "failed"]
    minimum: float
    unit_exit_code: int
    unit_attempt: int
    source_files: int
    totals: Metrics
    packages: dict[str, Metrics]
    tests: TestCounts


class IncompleteSummary(TypedDict):
    """Mark an unusable artifact explicitly rather than claiming complete coverage."""

    schema: int
    valid: Literal[False]
    status: Literal["incomplete"]


def counts(value: object) -> Counts:
    """Validate exact counters before aggregating or formatting them.

    Args:
        value: Counts mapping read from a coverage JSON artifact.

    Returns:
        Nonnegative counts with executed values no larger than measured values.

    Raises:
        ValueError: The mapping or a counter has an invalid shape or value.
    """
    if not isinstance(value, dict):
        raise ValueError("Invalid coverage counts")
    result: dict[str, int] = {}
    for name in COUNTERS:
        number = value.get(name)
        if type(number) is not int or number < 0:
            raise ValueError("Invalid coverage counts")
        result[name] = number
    if (
        result["covered_lines"] > result["num_statements"]
        or result["covered_branches"] > result["num_branches"]
    ):
        raise ValueError("Covered counts exceed measured code")
    return Counts(
        covered_lines=result["covered_lines"],
        num_statements=result["num_statements"],
        covered_branches=result["covered_branches"],
        num_branches=result["num_branches"],
    )


def percentages(value: Counts) -> Metrics:
    """Calculate presentation values while retaining every exact count.

    Args:
        value: Validated aggregate statement and branch counters.

    Returns:
        Exact counters plus line, branch and combined display percentages.
    """
    lines, branches = value["num_statements"], value["num_branches"]
    return {
        **value,
        "line_percent": 100 * value["covered_lines"] / lines if lines else 100.0,
        "branch_percent": 100 * value["covered_branches"] / branches if branches else 100.0,
        "combined_percent": 100
        * (value["covered_lines"] + value["covered_branches"])
        / (lines + branches)
        if lines + branches
        else 100.0,
    }


def measured_files(
    root: Path, members: list[str], files: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Map installed nested module names to source paths and verify the complete inventory.

    Args:
        root: Checkout root containing workspace package metadata and source directories.
        members: Backend workspaces that must all be represented in coverage.
        files: Coverage.py per-file data from installed or source module paths.

    Returns:
        Per-file data keyed by canonical checkout-relative source paths.

    Raises:
        ValueError: A source is absent, duplicated or not part of the backend inventory.
    """
    expected: set[str] = set()
    installed: dict[str, str] = {}
    for member in members:
        project = tomllib.loads((root / member / "pyproject.toml").read_text())
        namespace = project["project"]["name"].replace("-", "_")
        for path in (root / member / "src").rglob("*.py"):
            source = path.relative_to(root).as_posix()
            expected.add(source)
            installed[namespace + "/" + path.relative_to(root / member / "src").as_posix()] = source
    measured: dict[str, dict[str, Any]] = {}
    for filename, entry in files.items():
        normalized = filename.replace("\\", "/")
        observed_source = (
            normalized
            if normalized in expected
            else installed.get(normalized.split("site-packages/")[-1])
        )
        if observed_source is None or observed_source in measured or not isinstance(entry, dict):
            raise ValueError("Coverage inventory contains an unknown or duplicate module")
        measured[observed_source] = entry
    if not expected or set(measured) != expected:
        raise ValueError("Coverage inventory differs from backend source files")
    return measured


def summarize(root: Path, test_exit_code: int) -> Summary:
    """Validate full backend inventory, test results and counters before publishing a summary.

    Args:
        root: Checkout containing project configuration and generated JSON/JUnit artifacts.
        test_exit_code: Actual pytest process status, including coverage-gate failures.

    Returns:
        Validated summary with exact counters, display metrics and the producing attempt.

    Raises:
        ValueError: Coverage scope, inventory, counters or test outcomes are invalid.
        OSError: Required configuration or reporting artifacts cannot be read.
    """
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    if attempt < 1 or test_exit_code < 0:
        raise ValueError("Run attempt must be positive and test status nonnegative")
    config = tomllib.loads((root / "pyproject.toml").read_text())
    members: list[str] = config["tool"]["uv"]["workspace"]["members"]
    expected_packages = {
        tomllib.loads((root / member / "pyproject.toml").read_text())["project"]["name"].replace(
            "-", "_"
        )
        for member in members
    }
    options = shlex.split(config["tool"]["pytest"]["ini_options"]["addopts"])
    sources = [option.removeprefix("--cov=") for option in options if option.startswith("--cov=")]
    coverage = config["tool"]["coverage"]
    if (
        set(sources) != expected_packages
        or len(sources) != len(set(sources))
        or "--cov-branch" not in options
        or coverage.get("run", {}).get("omit")
        or coverage.get("report", {}).get("omit")
    ):
        raise ValueError(
            "Coverage must include every backend workspace with branches and no omissions"
        )
    minimum = coverage["report"]["fail_under"]
    if type(minimum) not in (int, float) or not math.isfinite(minimum) or not 0 < minimum <= 100:
        raise ValueError("Coverage minimum must be a finite percentage")
    document = json.loads((root / "reports/coverage.json").read_text())
    if document["meta"].get("branch_coverage") is not True:
        raise ValueError("Branch coverage was not measured")
    files = measured_files(root, members, document["files"])
    packages = {
        member: Counts(covered_lines=0, num_statements=0, covered_branches=0, num_branches=0)
        for member in sorted(members)
    }
    for filename, entry in files.items():
        member = next(member for member in members if filename.startswith(member + "/src/"))
        value = counts(entry["summary"])
        current = packages[member]
        packages[member] = counts({name: current[name] + value[name] for name in COUNTERS})
    total = counts({name: sum(package[name] for package in packages.values()) for name in COUNTERS})
    if counts(document["totals"]) != total or total["num_statements"] == 0:
        raise ValueError("Coverage totals do not match measured source")
    cases = list(ET.parse(root / "reports/junit.xml").getroot().iter("testcase"))
    if not cases:
        raise ValueError("No tests were reported")
    failed = sum(
        case.find("failure") is not None or case.find("error") is not None for case in cases
    )
    skipped = sum(case.find("skipped") is not None for case in cases)
    tests = TestCounts(
        total=len(cases), failed=failed, skipped=skipped, passed=len(cases) - failed - skipped
    )
    metrics = percentages(total)
    threshold_met = (
        (
            total["covered_lines"] == total["num_statements"]
            and total["covered_branches"] == total["num_branches"]
        )
        if minimum == 100
        else round(metrics["combined_percent"], 2) >= minimum
    )
    passed = test_exit_code == 0 and failed == 0 and skipped == 0 and threshold_met
    return {
        "schema": 1,
        "valid": True,
        "status": "passed" if passed else "failed",
        "minimum": float(minimum),
        "unit_exit_code": test_exit_code,
        "unit_attempt": attempt,
        "source_files": len(files),
        "totals": metrics,
        "packages": {member: percentages(value) for member, value in packages.items()},
        "tests": tests,
    }


def markdown(summary: Summary | IncompleteSummary) -> str:
    """Render the canonical report layout for job logs and the downloadable Markdown artifact.

    Args:
        summary: Validated coverage summary or explicit incomplete-artifact marker.

    Returns:
        Coverage status, threshold, test counts and percentage table for every workspace.
    """
    if summary["valid"] is False:
        return (
            "### Backend coverage\n\nCoverage report is missing or invalid. "
            "Inspect the Python job logs.\n"
        )
    total, tests = summary["totals"], summary["tests"]
    rows = [
        "### Backend coverage",
        "",
        f"**{summary['status'].upper()}** — minimum combined coverage "
        f"**{summary['minimum']:.2f}%**.",
        "",
        f"Tests: {tests['passed']} passed, {tests['failed']} failed, "
        f"{tests['skipped']} skipped. Source files: {summary['source_files']}.",
        "",
        "| Workspace | Lines | Branches | Combined |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, value in [("All backend code", total), *summary["packages"].items()]:
        rows.append(
            f"| {name} | {value['line_percent']:.2f}% | {value['branch_percent']:.2f}% | "
            f"{value['combined_percent']:.2f}% |"
        )
    rows.extend(
        [
            "",
            "Coverage includes every backend workspace. Unit and real-service integration "
            "tests run in the same suite.",
            "",
        ]
    )
    return "\n".join(rows)


def main() -> int:
    """Write valid or incomplete summary artifacts even when test or coverage checks fail.

    Returns:
        Zero only when tests and the configured coverage requirement both pass.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--test-exit-code", type=int, required=True)
    args = parser.parse_args()
    summary: Summary | IncompleteSummary
    try:
        summary = summarize(args.root, args.test_exit_code)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, StopIteration):
        summary = {"schema": 1, "valid": False, "status": "incomplete"}
    destination = args.root / "reports/coverage"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (destination / "summary.md").write_text(markdown(summary))
    print(markdown(summary))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
