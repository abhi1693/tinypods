"""In-process command checks complement the installed-process smoke tests."""

import re
import subprocess

import pytest
from tinypods_api import main as api
from tinypods_cli import main as cli
from tinypods_worker import main as worker
from typer.testing import CliRunner


def test_cli_diagnostics() -> None:
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert '"configuration": "valid"' in result.output


def test_worker_check(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["tinypods-worker", "--check"])
    worker.main()
    assert "configuration valid" in capsys.readouterr().out


@pytest.mark.parametrize(
    "entry,argv",
    [
        (api, ["tinypods-api"]),
        (cli, ["tinypods", "doctor"]),
        (worker, ["tinypods-worker", "--check"]),
    ],
)
def test_bad_settings_exit_safely(entry, argv, monkeypatch):
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setenv("TINYPODS_API_PORT", "private-credential")
    with pytest.raises(SystemExit) as caught:
        entry.main()
    assert str(caught.value) == "Invalid settings: api_port"


def test_server_uses_local_binding_and_disables_access_logs(monkeypatch):
    calls = []
    monkeypatch.setattr(api.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    api.main()
    assert calls[0]["host"] == "127.0.0.1"
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
    result = CliRunner().invoke(cli.app, arguments, color=True, env={"FORCE_COLOR": "1"})
    assert result.exit_code == 0
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert expected in plain_output


@pytest.mark.parametrize("arguments", [["db", "unknown"], ["doctor", "--unknown"], ["db"]])
def test_cli_invalid_command_has_clean_usage_error(arguments: list[str]) -> None:
    result = subprocess.run(
        ["tinypods", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
