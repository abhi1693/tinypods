"""Require complete statement and branch counts rather than rounded percentages."""

import argparse
import json
from pathlib import Path


def require_complete_coverage(path: Path) -> None:
    """Reject missing, branch-disabled, empty or incomplete coverage artifacts.

    Args:
        path: Coverage.py JSON report produced by the complete backend test run.

    Raises:
        OSError: The report cannot be read.
        ValueError: Coverage is disabled, malformed, empty or contains any misses.
    """
    report: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("Coverage report must be an object")
    metadata = report.get("meta")
    totals = report.get("totals")
    files = report.get("files")
    if not isinstance(metadata, dict) or metadata.get("branch_coverage") is not True:
        raise ValueError("Branch coverage must be enabled")
    if not isinstance(totals, dict) or not isinstance(files, dict) or not files:
        raise ValueError("Coverage must include measured source files and totals")
    counts: dict[str, int] = {}
    for key in (
        "num_statements",
        "covered_lines",
        "num_branches",
        "covered_branches",
        "missing_lines",
        "missing_branches",
    ):
        value = totals.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("Coverage counts must be nonnegative integers")
        counts[key] = value
    if not counts["num_statements"]:
        raise ValueError("Coverage must measure executable statements")
    if (
        counts["covered_lines"] != counts["num_statements"]
        or counts["covered_branches"] != counts["num_branches"]
        or counts["missing_lines"]
        or counts["missing_branches"]
    ):
        raise ValueError("Every measured statement and branch must be covered")
    for file in files.values():
        if not isinstance(file, dict):
            raise ValueError("Invalid per-file coverage")
        for key in ("missing_lines", "missing_branches"):
            missing = file.get(key)
            if not isinstance(missing, list) or missing:
                raise ValueError("Per-file coverage contains missing or invalid execution data")


def main() -> None:
    """Check the generated artifact and return a failing process status on any miss."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path, default=Path("reports/coverage.json"))
    args = parser.parse_args()
    try:
        require_complete_coverage(args.report)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Coverage check failed: {exc}\n")
    print("Complete statement and branch coverage confirmed")


if __name__ == "__main__":
    main()
