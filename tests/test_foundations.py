"""Installed entry points, configuration safety and process lifecycles."""

import asyncio
import importlib
import json
import logging
import os
import selectors
import signal
import socket
import subprocess
import time
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tinypods_api.main import create_app
from tinypods_core.logging import JsonFormatter, configure_logging
from tinypods_core.settings import ConfigurationError, Settings, load_settings


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited deployment settings so foundation tests use isolated defaults.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    for key in os.environ:
        if key.startswith("TINYPODS_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize(
    "distribution,module",
    [
        ("tinypods-api", "tinypods_api"),
        ("tinypods-worker", "tinypods_worker"),
        ("tinypods-cli", "tinypods_cli"),
        ("tinypods-core", "tinypods_core"),
    ],
)
def test_workspace_packages_are_installed(distribution: str, module: str) -> None:
    """Verify workspace packages are installed.

    Args:
        distribution: Installed workspace distribution whose metadata is checked.
        module: Import name of the installed workspace package.
    """
    assert version(distribution) == Path("VERSION").read_text().strip()
    assert importlib.import_module(module)


def test_environment_validation_redacts_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify environment validation redacts input.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    monkeypatch.setenv("TINYPODS_API_PORT", "private-credential")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert str(caught.value) == "Invalid settings: api_port"
    assert "private-credential" not in str(caught.value)


def test_valid_settings_and_secret_representations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify valid settings and secret representations.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    monkeypatch.setenv("TINYPODS_API_PORT", "8123")
    monkeypatch.setenv("TINYPODS_DATABASE_URL", "postgresql://private")
    settings = load_settings()
    assert settings.api_port == 8123
    assert "postgresql://private" not in repr(settings)


def test_json_logging_preserves_fields_and_exception_diagnostics() -> None:
    """Verify json logging preserves fields and exception diagnostics."""
    formatter = JsonFormatter("backend")
    record = logging.LogRecord(
        "tinypods.test", logging.ERROR, "", 1, "failure %s", ("private-value",), None
    )
    record.context = {
        "authorization": "bearer",
        "nested": [{"password": "hidden"}],
        "value": "private-value",
        "safe": 42,
    }
    record.exc_info = (ValueError, ValueError("private-value"), None)
    rendered = formatter.format(record)
    payload = json.loads(rendered)
    assert "private-value" in rendered
    assert "hidden" in rendered
    assert "bearer" in rendered
    assert payload["context"]["safe"] == 42
    assert payload["error_type"] == "ValueError"


def test_logging_configuration_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify logging configuration is idempotent.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    settings = Settings(log_format="json")
    configure_logging(settings)
    configure_logging(settings)
    logging.getLogger("tinypods.test").info("postgresql://private-value")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "postgresql://private-value"


def test_health_and_readiness_follow_lifecycle() -> None:
    """Verify health and readiness follow lifecycle."""
    app = create_app(Settings())
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "alive"}
        assert client.get("/readyz").status_code == 200
    assert not app.state.ready
    with TestClient(create_app(Settings())) as client:
        assert client.get("/openapi.json").json()["paths"]["/readyz"]["get"]["operationId"]
    unstarted = TestClient(create_app(Settings()))
    assert unstarted.get("/readyz").status_code == 503


@pytest.mark.parametrize("outcome", ["false", "exception", "timeout", "true"])
def test_readiness_failure_does_not_change_liveness(
    outcome: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify readiness failure does not change liveness.

    Args:
        outcome: Dependency result or failure mode simulated by the probe.
        capsys: Captured process stdout and stderr used for output assertions.
    """

    async def dependency() -> bool:
        """Simulate the selected readiness outcome under the application deadline.

        Returns:
            The simulated dependency availability result.
        """
        if outcome == "exception":
            raise RuntimeError("private-credential")
        if outcome == "timeout":
            await asyncio.sleep(1)
        return outcome == "true"

    app = create_app(Settings(readiness_timeout=0.01), readiness_checks={"storage": dependency})
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == (200 if outcome == "true" else 503)
        assert response.json()["checks"]["storage"] is (outcome == "true")
        assert client.get("/healthz").status_code == 200
        assert "private-credential" not in response.text
    assert "private-credential" not in capsys.readouterr().out


def test_runtime_readiness_name_cannot_be_overridden() -> None:
    """Verify runtime readiness name cannot be overridden."""
    with pytest.raises(ValueError, match="reserved"):
        create_app(Settings(), readiness_checks={"runtime": lambda: asyncio.sleep(0)})


@pytest.mark.parametrize("command", [["tinypods", "doctor"], ["tinypods-worker", "--check"]])
def test_installed_commands_validate_configuration(command: list[str]) -> None:
    """Verify installed commands validate configuration.

    Args:
        command: Installed command and arguments executed in a child process.
    """
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "valid" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "command", [["tinypods", "doctor"], ["tinypods-worker", "--check"], ["tinypods-api"]]
)
def test_entry_points_reject_bad_environment_without_secrets(command: list[str]) -> None:
    """Verify entry points reject bad environment without secrets.

    Args:
        command: Installed command and arguments executed in a child process.
    """
    environment = dict(os.environ, TINYPODS_API_PORT="private-credential")
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "api_port" in result.stderr
    assert "private-credential" not in result.stdout + result.stderr


def test_worker_stops_cleanly_on_sigterm() -> None:
    """Verify worker stops cleanly on sigterm."""
    process = subprocess.Popen(
        ["tinypods-worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "TINYPODS_WORKER_HEARTBEAT_INTERVAL": "0.05"},
    )
    try:
        assert process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            deadline = time.monotonic() + 5
            heartbeat = False
            while time.monotonic() < deadline:
                if selector.select(timeout=0.2):
                    line = process.stderr.readline()
                    if "heartbeat" in line:
                        heartbeat = True
                        break
            assert heartbeat, "Worker did not emit a heartbeat before the shutdown check"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "stopped" in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_api_entry_point_serves_http_and_shuts_down() -> None:
    """Verify api entry point serves http and shuts down."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        ["tinypods-api"],
        env=dict(os.environ, TINYPODS_API_PORT=str(port)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    assert json.load(response) == {"status": "alive"}
                break
            except URLError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("API did not become live")
                time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode in (0, -signal.SIGTERM), stderr
        assert "API stopped" in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("log_format", "yaml"),
        ("log_level", "TRACE"),
        ("database_mode", "unsupported"),
    ],
)
def test_literal_settings_reject_unsupported_values(field: str, value: str) -> None:
    """Keep runtime configuration constrained rather than weakening literal fields to strings.

    Args:
        field: Literal-constrained deployment setting.
        value: Unsupported value that must fail validation.
    """
    with pytest.raises(ValidationError) as failure:
        Settings.model_validate({field: value})
    assert failure.value.errors()[0]["type"] == "literal_error"
