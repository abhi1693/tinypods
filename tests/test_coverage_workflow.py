"""Exercise the data-only PR coverage publisher without GitHub writes or credentials."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ReportOutcome(TypedDict):
    """Capture mocked publication calls and workflow failure state."""

    created: list[str]
    updated: list[str]
    failures: list[str]
    summary: str


def workflow() -> dict[str, Any]:
    """Read workflow configuration as plain scalar data.

    Returns:
        Workflow jobs, steps and literal inline JavaScript, without YAML object construction.
    """
    return cast(
        dict[str, Any],
        yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader),
    )


def sample_report() -> dict[str, Any]:
    """Build a validated summary artifact matching the canonical reporting schema.

    Returns:
        Consistent per-workspace counts, producing attempt and complete JUnit totals.
    """
    counters = {"covered_lines": 1, "num_statements": 2, "covered_branches": 1, "num_branches": 2}
    return {
        "schema": 1,
        "valid": True,
        "status": "passed",
        "minimum": 50.0,
        "unit_exit_code": 0,
        "unit_attempt": 1,
        "source_files": 4,
        "totals": {key: value * 4 for key, value in counters.items()},
        "packages": {
            name: counters.copy()
            for name in ("apps/api", "apps/cli", "apps/worker", "packages/core")
        },
        "tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0},
    }


def publish(
    tmp_path: Path,
    report: str | None,
    *,
    existing: bool = False,
    stale: str = "never",
    result: str = "success",
) -> ReportOutcome:
    """Run the exact workflow script with local artifacts and an in-memory GitHub client.

    Args:
        tmp_path: Disposable directory used as the report job working directory.
        report: Raw report content, or None to simulate a missing artifact.
        existing: Whether a prior bot-owned coverage comment is already present.
        stale: Whether the PR head changes before processing or just before publication.
        result: Result of the backend job that produced the artifact.

    Returns:
        Recorded comment creates/updates, job summary and explicit reporting failures.
    """
    if report is not None:
        directory = tmp_path / "report"
        directory.mkdir(exist_ok=True)
        (directory / "coverage").mkdir(exist_ok=True)
        (directory / "coverage/summary.json").write_text(report)
    script = workflow()["jobs"]["coverage-report"]["steps"][1]["with"]["script"]
    runner = tmp_path / "publisher.cjs"
    runner.write_text(
        """
const outcome = {created: [], updated: [], failures: [], summary: ''};
let calls = 0;
const head = 'a'.repeat(40);
const context = {repo: {owner: 'owner', repo: 'tinypods'}, runId: 1,
  serverUrl: 'https://github.com', payload: {pull_request: {number: 1, head: {sha: head}}}};
const github = {rest: {pulls: {get: async () => {
  calls++;
  const changed = process.env.STALE === 'early' || (process.env.STALE === 'late' && calls > 1);
  return {data: {head: {sha: changed ? 'b'.repeat(40) : head}}};
}}, issues: {
  listComments: () => {},
  createComment: async ({body}) => outcome.created.push(body),
  updateComment: async ({body}) => outcome.updated.push(body),
}}, paginate: async () => process.env.EXISTING === 'true' ? [
  {id: 7, user: {type: 'Bot'}, body: '<!-- tinypods-backend-coverage --> previous'}
] : []};
const core = {setFailed: message => outcome.failures.push(message),
  summary: {addRaw: body => {outcome.summary = body; return core.summary;}, write: async () => {}}};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
"""
        + "const script = "
        + json.dumps(script)
        + ";\n"
        + """
new AsyncFunction('github', 'context', 'core', 'require', script)(github, context, core, require)
  .then(() => console.log(JSON.stringify(outcome)))
  .catch(error => {console.error(error); process.exit(1);});
"""
    )
    command = subprocess.run(
        ["node", str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "BACKEND_RESULT": result,
            "GITHUB_RUN_ATTEMPT": "1",
            "EXISTING": str(existing).lower(),
            "STALE": stale,
        },
    )
    assert command.returncode == 0, command.stderr
    return cast(ReportOutcome, json.loads(command.stdout))


@pytest.mark.parametrize("existing", [False, True])
def test_coverage_comment_creates_or_updates_one_report(tmp_path: Path, existing: bool) -> None:
    """Publish the established status, test counts and percentage table without duplication.

    Args:
        tmp_path: Isolated publisher directory.
        existing: Whether to update the previous bot-owned report.
    """
    outcome = publish(tmp_path, json.dumps(sample_report()), existing=existing)
    bodies = outcome["updated"] if existing else outcome["created"]
    assert len(bodies) == 1 and not outcome["failures"]
    assert not (outcome["created"] if existing else outcome["updated"])
    body = bodies[0]
    assert "50.00%" in body and "minimum combined coverage **50.00%**" in body
    assert "Tests: 1 passed, 0 failed, 0 skipped. Source files: 4." in body
    assert "| Workspace | Lines | Branches | Combined |" in body


@pytest.mark.parametrize("stale", ["early", "late"])
def test_coverage_comment_ignores_superseded_heads(tmp_path: Path, stale: str) -> None:
    """Avoid publishing a report for an older PR commit.

    Args:
        tmp_path: Isolated publisher directory.
        stale: Point at which the mocked head changes.
    """
    outcome = publish(tmp_path, json.dumps(sample_report()), stale=stale)
    assert not outcome["created"] and not outcome["updated"]


@pytest.mark.parametrize(
    "case", ["missing", "json", "oversized", "totals", "workspace", "attempt", "tests", "counts"]
)
def test_coverage_comment_fails_closed_for_invalid_reports(tmp_path: Path, case: str) -> None:
    """Expose unavailable coverage rather than presenting malformed artifacts as a pass.

    Args:
        tmp_path: Isolated publisher directory.
        case: Malformed or incomplete artifact scenario.
    """
    report = sample_report()
    raw: str | None
    if case == "missing":
        raw = None
    elif case == "json":
        raw = "not JSON"
    elif case == "oversized":
        raw = " " * 2000001
    else:
        if case == "totals":
            report["totals"]["covered_lines"] += 1
        elif case == "workspace":
            del report["packages"]["apps/api"]
        elif case == "attempt":
            report["unit_attempt"] = 2
        elif case == "tests":
            report["tests"]["failed"] = 2
        elif case == "counts":
            report["totals"]["covered_branches"] = 100
        raw = json.dumps(report)
    outcome = publish(tmp_path, raw)
    assert outcome["failures"] and "Coverage unavailable" in outcome["created"][0]
    assert "**PASS**" not in outcome["created"][0]


def test_coverage_report_does_not_hide_backend_failure(tmp_path: Path) -> None:
    """Keep real measurements visible while marking the producing backend job as failed.

    Args:
        tmp_path: Isolated publisher directory.
    """
    outcome = publish(tmp_path, json.dumps(sample_report()), result="failure")
    assert "**FAIL**" in outcome["created"][0] and "50.00%" in outcome["created"][0]


def test_coverage_workflow_keeps_privileged_job_data_only() -> None:
    """Constrain publication to trusted PRs without executing their checkout in that job."""
    config = workflow()
    assert set(config["on"]) == {"pull_request", "push"}
    report = config["jobs"]["coverage-report"]
    assert report["needs"] == "python"
    assert "head.repo.full_name == github.repository" in report["if"]
    assert "dependabot[bot]" in report["if"]
    assert report["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert all(
        "run" not in step and "checkout" not in step.get("uses", "") for step in report["steps"]
    )
    assert config["permissions"] == {"contents": "read"}
