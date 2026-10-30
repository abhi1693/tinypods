"""In-process command checks complement the installed-process smoke tests."""

import pytest
from tinypods_api import main as api
from tinypods_cli import main as cli
from tinypods_worker import main as worker


def test_cli_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["tinypods", "doctor"])
    cli.main()
    assert '"configuration": "valid"' in capsys.readouterr().out


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
