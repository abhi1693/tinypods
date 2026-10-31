import os
import subprocess
import tomllib
from importlib.metadata import version
from pathlib import Path

from tinypods_api.main import create_app
from tinypods_core import __version__
from tinypods_core.settings import Settings

SCRIPT = Path("scripts/sync_version.py").resolve()


def test_shared_version_matches_packages_and_openapi():
    expected = Path("VERSION").read_text().strip()
    assert __version__ == expected
    for project in [
        Path("pyproject.toml"),
        *Path("apps").glob("*/pyproject.toml"),
        *Path("packages").glob("*/pyproject.toml"),
    ]:
        metadata = tomllib.loads(project.read_text())["project"]
        assert metadata["version"] == expected
        if metadata["name"] != "tinypods-workspace":
            assert version(metadata["name"]) == expected
    assert create_app(Settings()).openapi()["info"]["version"] == expected


def test_sync_updates_only_project_versions_and_detects_drift(tmp_path):
    (tmp_path / "VERSION").write_text("0.0.2")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "tinypods-test"\nversion = "0.0.1"\n'
        'dependencies = ["third-party==9.8.7"]\n'
        '[tool.uv.workspace]\nmembers = ["apps/*"]\n'
    )
    member = tmp_path / "apps/api"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname = "tinypods-api"\nversion = "0.0.1"\n')
    command = ["python", str(SCRIPT), "--root", str(tmp_path)]
    assert subprocess.run(command + ["--check"], capture_output=True).returncode == 1
    assert subprocess.run(command, capture_output=True).returncode == 0
    for project in (tmp_path / "pyproject.toml", member / "pyproject.toml"):
        assert tomllib.loads(project.read_text())["project"]["version"] == "0.0.2"
    assert "third-party==9.8.7" in (tmp_path / "pyproject.toml").read_text()
    assert subprocess.run(command + ["--check"], capture_output=True).returncode == 0
    (tmp_path / "VERSION").write_text("invalid")
    assert subprocess.run(command, capture_output=True).returncode == 2


def test_runtime_version_does_not_depend_on_checkout(tmp_path):
    result = subprocess.run(
        ["python", "-c", "from tinypods_core import __version__; print(__version__)"],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == __version__
