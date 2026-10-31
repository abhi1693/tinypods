"""In-process command checks complement the installed-process smoke tests."""

import re
import subprocess
from types import ModuleType

import pytest
from tinypods_api import main as api
from tinypods_cli import main as cli
from tinypods_worker import main as worker
from typer.testing import CliRunner


def test_cli_diagnostics() -> None:
    """Verify cli diagnostics."""
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert '"configuration": "valid"' in result.output


def test_worker_check(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify worker check.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
        capsys: Captured process stdout and stderr used for output assertions.
    """
    monkeypatch.setattr("sys.argv", ["tinypods-worker", "--check"])
    worker.main()
    assert "configuration valid" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entry,argv",
    [
        (api, ["tinypods-api"]),
        (cli, ["tinypods", "doctor"]),
        (worker, ["tinypods-worker", "--check"]),
    ],
)
def test_bad_settings_exit_safely(
    entry: ModuleType, argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify bad settings exit safely.

    Args:
        entry: Imported process entry-point module under test.
        argv: Process arguments supplied to the command under test.
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setenv("TINYPODS_API_PORT", "private-credential")
    with pytest.raises(SystemExit) as caught:
        entry.main()
    assert str(caught.value) == "Invalid settings: api_port"


def test_server_uses_all_interfaces_and_disables_access_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify server uses all interfaces and disables access logs.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    calls = []
    monkeypatch.setattr(api.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    api.main()
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["access_log"] is False


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["--help"], "doctor"),
        (["db", "--help"], "upgrade"),
        (["doctor", "--help"], "--database"),
        (["db", "upgrade", "--help"], "--config"),
    ],
)
def test_cli_help_is_available_without_database(
    arguments: list[str],
    expected: str,
) -> None:
    """Verify cli help is available without database.

    Args:
        arguments: Compose subcommand and flags for this isolated project.
        expected: Expected observable result for the parameterized case.
    """
    result = CliRunner().invoke(cli.app, arguments, color=True, env={"FORCE_COLOR": "1"})
    assert result.exit_code == 0
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert expected in plain_output


@pytest.mark.parametrize("arguments", [["db", "unknown"], ["doctor", "--unknown"], ["db"]])
def test_cli_invalid_command_has_clean_usage_error(arguments: list[str]) -> None:
    """Verify cli invalid command has clean usage error.

    Args:
        arguments: Compose subcommand and flags for this isolated project.
    """
    result = subprocess.run(
        ["tinypods", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_worker_check_closes_owned_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Release process-owned logging when a worker configuration check returns.

    Args:
        monkeypatch: Temporary command-line substitution.

    Returns:
        None. Unrelated handlers remain while TinyPods handlers are removed.
    """
    import logging

    from tinypods_core.logging import StderrHandler

    monkeypatch.setattr("sys.argv", ["tinypods-worker", "--check"])
    worker.main()
    assert not any(isinstance(handler, StderrHandler) for handler in logging.getLogger().handlers)
