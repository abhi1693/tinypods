"""Prevent unsafe source data from entering request and command log records."""

import logging
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient
from tinypods_api.main import create_app
from tinypods_cli import main as cli
from tinypods_cli.runtime import command_settings
from tinypods_core.settings import Settings


@pytest.mark.parametrize("boundary", ["api", "cli"])
@pytest.mark.parametrize("format_name", ["json", "text"])
def test_boundary_failures_log_only_deliberate_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
    format_name: str,
) -> None:
    """Exclude unsafe exception text, headers, query and body values before formatting.

    Args:
        monkeypatch: Scoped command and process configuration substitutions.
        caplog: Raw records captured before output formatting.
        capsys: Captured process output used to verify both renderers.
        boundary: Request middleware or command entry point under test.
        format_name: Selected output renderer.
    """
    secret = "synthetic-credential-never-submit-to-logging"
    monkeypatch.setenv("TINYPODS_LOG_FORMAT", format_name)
    caplog.set_level(logging.INFO)
    if boundary == "api":
        app = create_app(Settings(log_format=format_name))

        @app.post("/sensitive-failure", response_model=None)
        async def fail_request() -> NoReturn:
            """Simulate a dependency exception containing sensitive source input.

            Raises:
                RuntimeError: Always, with a synthetic credential in its message.
            """
            raise RuntimeError(secret)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/sensitive-failure?token=" + secret,
                headers={"Authorization": "Bearer " + secret},
                json={"password": secret},
            )
        assert response.status_code == 500 and secret not in response.text
        event = "request_failed"
    else:

        def fail_command() -> NoReturn:
            """Simulate an unsafe command failure before deliberate boundary logging.

            Raises:
                RuntimeError: Always, with a synthetic credential in its message.
            """
            command_settings()
            raise RuntimeError(secret)

        monkeypatch.setattr(cli, "app", fail_command)
        with pytest.raises(SystemExit, match="Command failed"):
            cli.main()
        event = "command_failed"
    failures = [record for record in caplog.records if record.msg == event]
    assert len(failures) == 1
    record = failures[0]
    assert record.exc_info is None and not record.args
    assert record.error_type == "RuntimeError" and record.traceback
    assert secret not in repr(record.__dict__)
    output = capsys.readouterr()
    assert secret not in output.err and secret not in output.out
