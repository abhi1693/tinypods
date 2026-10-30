import asyncio
import importlib
import json
import logging
import os
import signal
import socket
import subprocess
import time
from importlib.metadata import version
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from tinypods_api.main import create_app
from tinypods_core.logging import JsonFormatter, configure_logging
from tinypods_core.settings import ConfigurationError, Settings, load_settings


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
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
def test_workspace_packages_are_installed(distribution, module):
    assert version(distribution) == "0.1.0"
    assert importlib.import_module(module)


def test_environment_validation_redacts_input(monkeypatch):
    monkeypatch.setenv("TINYPODS_API_PORT", "private-credential")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert str(caught.value) == "Invalid settings: api_port"
    assert "private-credential" not in str(caught.value)


def test_valid_settings_and_secret_representations(monkeypatch):
    monkeypatch.setenv("TINYPODS_API_PORT", "8123")
    monkeypatch.setenv("TINYPODS_DATABASE_URL", "postgresql://private")
    settings = load_settings()
    assert settings.api_port == 8123
    assert "postgresql://private" not in repr(settings)
    assert settings.secret_values() == ("postgresql://private",)


def test_json_logging_redacts_nested_context_and_exceptions():
    formatter = JsonFormatter(("private-value",))
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
    assert "private-value" not in rendered
    assert "hidden" not in rendered
    assert "bearer" not in rendered
    assert payload["context"]["safe"] == 42
    assert payload["exception_type"] == "ValueError"


def test_logging_configuration_is_idempotent(capsys):
    settings = Settings(database_url=SecretStr("private-value"))
    configure_logging(settings)
    configure_logging(settings)
    logging.getLogger("tinypods.test").info("private-value")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "[REDACTED]"


def test_health_and_readiness_follow_lifecycle():
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
def test_readiness_failure_does_not_change_liveness(outcome, capsys):
    async def dependency():
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


def test_runtime_readiness_name_cannot_be_overridden():
    with pytest.raises(ValueError, match="reserved"):
        create_app(Settings(), readiness_checks={"runtime": lambda: asyncio.sleep(0)})


@pytest.mark.parametrize("command", [["tinypods", "doctor"], ["tinypods-worker", "--check"]])
def test_installed_commands_validate_configuration(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "valid" in result.stdout


@pytest.mark.parametrize(
    "command", [["tinypods", "doctor"], ["tinypods-worker", "--check"], ["tinypods-api"]]
)
def test_entry_points_reject_bad_environment_without_secrets(command):
    environment = dict(os.environ, TINYPODS_API_PORT="private-credential")
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "api_port" in result.stderr
    assert "private-credential" not in result.stdout + result.stderr


def test_worker_stops_cleanly_on_sigterm():
    process = subprocess.Popen(
        ["tinypods-worker"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert "started" in process.stdout.readline()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "stopped" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_api_entry_point_serves_http_and_shuts_down():
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
        assert "API stopped" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
