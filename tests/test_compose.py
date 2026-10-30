"""Exercise the shipped Compose files with isolated project names and volumes."""

import json
import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ComposeStack:
    name: str
    environment: dict[str, str] = field(repr=False)
    extra_files: tuple[Path, ...] = ()

    def run(self, *arguments: str, timeout: int = 120) -> str:
        files = [ROOT / "compose.yaml", ROOT / "compose.build.yaml", ROOT / "compose.workers.yaml"]
        files.extend(self.extra_files)
        command = ["docker", "compose", "--project-name", self.name, "--env-file", "/dev/null"]
        for path in files:
            command.extend(["-f", str(path)])
        result = subprocess.run(
            command + list(arguments),
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        return result.stdout.strip()

    def identity(self) -> str:
        return self.run("exec", "-T", "api", "tinypods", "doctor", "--database")

    def assert_ready(self) -> None:
        port = self.run("port", "api", "8000").rsplit(":", 1)[1]
        with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as response:
            assert json.load(response)["status"] == "ready"


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[ComposeStack]:
    name = "tinypods-compose-tests-" + uuid4().hex[:10]
    password, queue_password = uuid4().hex, uuid4().hex
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TINYPODS_", "COMPOSE_", "POSTGRES_", "VALKEY_"))
    }
    environment.update(
        {
            "COMPOSE_PROFILES": "local",
            "POSTGRES_PASSWORD": password,
            "VALKEY_PASSWORD": queue_password,
            "TINYPODS_DATABASE_URL": f"postgresql+psycopg://tinypods:{password}@postgres:5432/tinypods",
            "TINYPODS_VALKEY_URL": f"redis://:{queue_password}@valkey:6379/0",
            "TINYPODS_BACKEND_IMAGE": "tinypods-tests/backend:" + name,
            "TINYPODS_PUBLISH_HOST": "127.0.0.1",
            "TINYPODS_PUBLISH_PORT": "0",
            "TINYPODS_WORKER_HEARTBEAT_INTERVAL": "0.1",
        }
    )
    stack = ComposeStack(name, environment)
    try:
        stack.run("build", "api", timeout=300)
        stack.run("up", "-d", "--wait", "--wait-timeout", "90")
        yield stack
    finally:
        # Only this generated project's containers/networks/volumes are disposable.
        stack.run("down", "--volumes", "--remove-orphans")
        subprocess.run(
            ["docker", "image", "rm", environment["TINYPODS_BACKEND_IMAGE"]],
            capture_output=True,
            timeout=30,
        )


def test_compose_startup_health_and_private_services(compose_stack: ComposeStack) -> None:
    stack = compose_stack
    stack.assert_ready()
    assert "installation_id" in json.loads(stack.identity())
    configuration = json.loads(stack.run("config", "--format", "json"))
    services = configuration["services"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["valkey"]
    assert configuration["networks"]["data"]["internal"] is True
    assert services["worker"]["scale"] == 1
    assert services["api"]["environment"]["TINYPODS_API_HOST"] == "0.0.0.0"
    assert stack.run("exec", "-T", "api", "id", "-u") == "10001"
    deadline = time.monotonic() + 5
    while "Worker process heartbeat" not in stack.run("logs", "--no-color", "worker"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    for key, expected in [
        ("appendonly", "yes"),
        ("maxmemory-policy", "noeviction"),
        ("maxmemory", "134217728"),
    ]:
        assert expected in stack.run("exec", "-T", "valkey", "valkey-cli", "CONFIG", "GET", key)


def test_compose_recreation_preserves_database_and_queue(compose_stack: ComposeStack) -> None:
    stack = compose_stack
    identity = json.loads(stack.identity())
    stack.run("exec", "-T", "valkey", "valkey-cli", "SET", "tinypods:test:persist", "retained")
    stack.run("down")
    stack.run("up", "-d", "--wait", "--wait-timeout", "90")
    stack.assert_ready()
    assert json.loads(stack.identity()) == identity
    assert (
        stack.run("exec", "-T", "valkey", "valkey-cli", "GET", "tinypods:test:persist")
        == "retained"
    )


def test_external_services_do_not_start_local_dependencies(
    compose_stack: ComposeStack,
    tmp_path: Path,
) -> None:
    # Reuse only this test's infrastructure through its network as external services.
    override = tmp_path / "external.yaml"
    override.write_text(
        f"networks:\n  data: !override\n    external: true\n    name: {compose_stack.name}_data\n"
    )
    environment = dict(compose_stack.environment, COMPOSE_PROFILES="")
    stack = ComposeStack(compose_stack.name + "-external", environment, (override,))
    try:
        stack.run("up", "-d", "--wait", "--wait-timeout", "90")
        stack.assert_ready()
        assert json.loads(stack.identity()) == json.loads(compose_stack.identity())
        services = stack.run("ps", "--services", "--all").splitlines()
        assert set(services) == {"api", "worker", "migrate"}
    finally:
        stack.run("down", "--volumes", "--remove-orphans")


def test_publish_host_and_port_are_configurable(compose_stack: ComposeStack) -> None:
    environment = dict(compose_stack.environment)
    environment.pop("TINYPODS_PUBLISH_HOST")
    environment.pop("TINYPODS_PUBLISH_PORT")
    stack = ComposeStack(compose_stack.name, environment)
    configuration = json.loads(stack.run("config", "--format", "json"))
    port = configuration["services"]["api"]["ports"][0]
    assert port["host_ip"] == "0.0.0.0"
    assert port["published"] == "8000"
    environment.update(TINYPODS_PUBLISH_HOST="192.0.2.10", TINYPODS_PUBLISH_PORT="8123")
    configuration = json.loads(stack.run("config", "--format", "json"))
    port = configuration["services"]["api"]["ports"][0]
    assert port["host_ip"] == "192.0.2.10"
    assert port["published"] == "8123"
