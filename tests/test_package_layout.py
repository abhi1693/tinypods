"""Validate installed public imports and complete wheel discovery after domain grouping."""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = {
    "tinypods_core": "packages/core",
    "tinypods_api": "apps/api",
    "tinypods_cli": "apps/cli",
    "tinypods_worker": "apps/worker",
}
PUBLIC_PACKAGES = (
    "tinypods_core",
    "tinypods_core.cache",
    "tinypods_core.auth",
    "tinypods_core.auth.providers",
    "tinypods_core.auth.providers.local",
    "tinypods_core.auth.providers.zitadel",
    "tinypods_core.rate_limit",
    "tinypods_core.db",
    "tinypods_core.logging",
    "tinypods_api",
    "tinypods_api.cache",
    "tinypods_api.auth",
    "tinypods_api.auth.providers",
    "tinypods_api.auth.providers.local",
    "tinypods_api.auth.providers.zitadel",
    "tinypods_api.rate_limit",
    "tinypods_api.http",
    "tinypods_cli",
    "tinypods_cli.commands",
    "tinypods_worker",
)


@pytest.fixture(scope="module")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build every distribution from its source archive using the registered subpackages.

    Args:
        tmp_path_factory: Pytest-owned directory factory for disposable build artifacts.

    Returns:
        Directory containing all four source distributions and their rebuilt wheels.
    """
    directory = tmp_path_factory.mktemp("domain-package-builds")
    result = subprocess.run(
        ["uv", "build", "--all-packages", "--out-dir", str(directory)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return directory


@pytest.mark.parametrize("distribution", PROJECTS)
def test_wheels_include_every_domain_module_and_typing_marker(
    wheelhouse: Path,
    distribution: str,
) -> None:
    """Catch omitted package registration and stale renamed modules in real build output.

    Args:
        wheelhouse: Complete wheels rebuilt from disposable source distributions.
        distribution: Workspace distribution whose namespace is checked.
    """
    source = ROOT / PROJECTS[distribution] / "src"
    expected = {distribution + "/" + str(path.relative_to(source)) for path in source.rglob("*.py")}
    wheel = next(wheelhouse.glob(distribution + "-*.whl"))
    assert next(wheelhouse.glob(distribution + "-*.tar.gz")).is_file()
    with zipfile.ZipFile(wheel) as archive:
        actual = {
            name
            for name in archive.namelist()
            if name.startswith(distribution + "/") and name.endswith(".py")
        }
        assert actual == expected
        assert distribution + "/py.typed" in archive.namelist()


@pytest.mark.parametrize("package", PUBLIC_PACKAGES)
def test_public_exports_import_independently_outside_checkout(tmp_path: Path, package: str) -> None:
    """Resolve each intentional public API in a fresh interpreter without checkout imports.

    Args:
        tmp_path: Working directory outside the checkout for an isolated Python process.
        package: Domain namespace whose explicit public import surface is exercised.
    """
    code = """
import importlib, os, sys
os.environ['TINYPODS_API_PORT'] = 'invalid-on-purpose'
module = importlib.import_module(sys.argv[1])
assert module.__all__ and len(module.__all__) == len(set(module.__all__))
namespace = {}
exec('from ' + sys.argv[1] + ' import *', namespace)
assert set(namespace) - {'__builtins__'} == set(module.__all__)
for name in module.__all__:
    assert namespace[name] is getattr(module, name)
    assert not name.startswith('_') or name == '__version__'
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, package],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""
