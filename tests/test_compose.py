"""Exercise the shipped Compose files with isolated project names and volumes."""

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ComposeStack:
    """Own the generated name, environment and configuration of a disposable Compose project."""

    name: str
    environment: dict[str, str] = field(repr=False)
    extra_files: tuple[Path, ...] = ()
    directory: Path = ROOT

    def command(self, *arguments: str) -> list[str]:
        """Build a Compose invocation using explicit files and no operator environment file.

        Args:
            arguments: Compose subcommand and flags for this isolated project.

        Returns:
            Executable and explicit Compose arguments.
        """
        files = [
            self.directory / name
            for name in ["compose.yaml", "compose.build.yaml", "compose.workers.yaml"]
        ]
        files.extend(self.extra_files)
        command = ["docker", "compose", "--project-name", self.name, "--env-file", "/dev/null"]
        for path in files:
            command.extend(["-f", str(path)])
        return command + list(arguments)

    def run(self, *arguments: str, timeout: int = 120) -> str:
        """Run Compose with bounded execution and include diagnostic output on failure.

        Args:
            timeout: Maximum subprocess execution time in seconds.
            arguments: Compose subcommand and flags for this isolated project.

        Returns:
            Captured subprocess output or the formatted value described above.
        """
        result = subprocess.run(
            self.command(*arguments),
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        return result.stdout.strip()

    def identity(self) -> str:
        """Read the migrated installation identity through the API container CLI.

        Returns:
            Captured subprocess output or the formatted value described above.
        """
        return self.run("exec", "-T", "api", "tinypods", "doctor", "--database")

    def assert_ready(self) -> None:
        """Assert that the published loopback readiness endpoint reports ready."""
        port = self.run("port", "api", "8000").rsplit(":", 1)[1]
        with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as response:
            assert json.load(response)["status"] == "ready"


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[ComposeStack]:
    """Build and start an isolated stack, then remove only its generated resources.

    Yields:
        Started stack; teardown removes only its generated resources.
    """
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
            "TINYPODS_LOCAL_IMAGE_PREFIX": "tinypods-tests/" + name,
            "TINYPODS_PUBLISH_HOST": "127.0.0.1",
            "TINYPODS_PUBLISH_PORT": "0",
            "TINYPODS_WORKER_HEARTBEAT_INTERVAL": "0.1",
        }
    )
    stack = ComposeStack(name, environment)
    try:
        stack.run("build", timeout=300)
        stack.run("up", "-d", "--wait", "--wait-timeout", "90")
        yield stack
    finally:
        # Only this generated project's containers/networks/volumes are disposable.
        stack.run("down", "--volumes", "--remove-orphans")
        subprocess.run(
            [
                "docker",
                "image",
                "rm",
                *[
                    environment["TINYPODS_LOCAL_IMAGE_PREFIX"] + "/" + service + ":local"
                    for service in ("api", "worker", "migrate")
                ],
            ],
            capture_output=True,
            timeout=30,
        )


def test_compose_startup_health_and_private_services(compose_stack: ComposeStack) -> None:
    """Verify startup, private networking, non-root services and live response caching.

    Args:
        compose_stack: Isolated built Compose project with disposable data volumes.

    Returns:
        None. Assertions cover service health, ownership and cache MISS/HIT behavior.
    """
    stack = compose_stack
    stack.assert_ready()
    assert "installation_id" in json.loads(stack.identity())
    configuration = json.loads(stack.run("config", "--format", "json"))
    services = configuration["services"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["valkey"]
    assert services["valkey"]["user"] == "999:999"
    assert services["valkey"]["read_only"] is True
    assert services["valkey"]["cap_drop"] == ["ALL"]
    assert stack.run("exec", "-T", "valkey", "id", "-u") == "999"
    assert services["api"]["environment"]["TINYPODS_CACHE_ENABLED"] == "true"
    port = stack.run("port", "api", "8000").rsplit(":", 1)[1]
    with urlopen(f"http://127.0.0.1:{port}/info", timeout=5) as response:
        assert response.headers["X-Cache"] == "MISS"
    with urlopen(f"http://127.0.0.1:{port}/info", timeout=5) as response:
        assert response.headers["X-Cache"] == "HIT"
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
    """Verify compose recreation preserves database and queue.

    Args:
        compose_stack: Isolated Compose project with generated credentials and disposable volumes.
    """
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
    """Verify external services do not start local dependencies.

    Args:
        compose_stack: Isolated Compose project with generated credentials and disposable volumes.
        tmp_path: Pytest-owned temporary directory for disposable artifacts.
    """
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
    """Verify publish host and port are configurable.

    Args:
        compose_stack: Isolated Compose project with generated credentials and disposable volumes.
    """
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


def test_optional_registry_rewrites_upstream_images_and_build_args(
    compose_stack: ComposeStack,
) -> None:
    """Verify optional registry rewrites upstream images and build args.

    Args:
        compose_stack: Isolated Compose project with generated credentials and disposable volumes.
    """
    stack = ComposeStack(
        compose_stack.name, dict(compose_stack.environment, TINYPODS_IMAGE_REGISTRY="registry.home")
    )
    services = json.loads(stack.run("config", "--format", "json"))["services"]
    assert services["postgres"]["image"].startswith("registry.home/docker.io/library/postgres:")
    assert services["valkey"]["image"].startswith("registry.home/docker.io/valkey/valkey:")
    for service in ["api", "worker", "migrate"]:
        assert services[service]["build"]["args"] == {
            "DOCKERHUB_PREFIX": "registry.home/docker.io/library/",
            "REGISTRY_PREFIX": "registry.home/",
        }
    original = json.loads(compose_stack.run("config", "--format", "json"))["services"]
    assert original["postgres"]["image"].startswith("postgres:")
    assert original["valkey"]["image"].startswith("valkey/valkey:")
    assert original["api"]["build"]["args"]["REGISTRY_PREFIX"] == ""


def test_watch_rebuilds_api_worker_and_runs_new_migrations(
    compose_stack: ComposeStack,
    tmp_path: Path,
) -> None:
    """Verify watch rebuilds api worker and runs new migrations.

    Args:
        compose_stack: Isolated Compose project with generated credentials and disposable volumes.
        tmp_path: Pytest-owned temporary directory for disposable artifacts.
    """
    directory = tmp_path / "checkout"
    directory.mkdir()
    for name in ["apps", "packages", "migrations"]:
        shutil.copytree(
            ROOT / name,
            directory / name,
            ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"),
        )
    for name in [
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "alembic.ini",
        "compose.yaml",
        "compose.build.yaml",
        "compose.workers.yaml",
    ]:
        shutil.copy2(ROOT / name, directory / name)
    probe = directory / "packages/core/src/watch_probe.py"
    probe.write_text('VALUE = "before"\n')
    environment = dict(compose_stack.environment)
    environment["TINYPODS_LOCAL_IMAGE_PREFIX"] += "-watch"
    stack = ComposeStack(compose_stack.name + "-watch", environment, directory=directory)
    watcher: subprocess.Popen[bytes] | None = None
    output = tmp_path / "watch.log"
    try:
        stack.run("build", timeout=300)
        stack.run("up", "-d", "--wait", "--wait-timeout", "90")
        original = json.loads(stack.identity())
        with output.open("wb") as log:
            watcher = subprocess.Popen(
                stack.command("watch", "--no-up"),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 20
            while "Watch enabled" not in output.read_text():
                assert watcher.poll() is None, output.read_text()[-3000:]
                assert time.monotonic() < deadline, output.read_text()[-3000:]
                time.sleep(0.1)
            probe.write_text('VALUE = "after"\n')
            (directory / "migrations/versions/0002_watch.py").write_text(
                "from alembic import op\nrevision='0002_watch'\n"
                "down_revision='0001_installation'\n"
                "def upgrade():\n    op.execute('CREATE TABLE compose_watch_probe (id integer)')\n"
            )
            deadline = time.monotonic() + 150
            while True:
                assert watcher.poll() is None, output.read_text()[-3000:]
                try:
                    values = [
                        stack.run(
                            "exec",
                            "-T",
                            service,
                            "python",
                            "-c",
                            "from tinypods_core.watch_probe import VALUE; print(VALUE)",
                        )
                        for service in ["api", "worker"]
                    ]
                    revisions = json.loads(
                        stack.run("exec", "-T", "api", "tinypods", "db", "current")
                    )["revisions"]
                    if values == ["after", "after"] and revisions == ["0002_watch"]:
                        break
                except (AssertionError, json.JSONDecodeError):
                    pass
                assert time.monotonic() < deadline, output.read_text()[-5000:]
                time.sleep(0.5)
            stack.assert_ready()
            assert json.loads(stack.identity()) == original
            ids = stack.run("ps", "-aq").splitlines()
            containers = json.loads(subprocess.check_output(["docker", "inspect", *ids]))
            states = {
                item["Config"]["Labels"]["com.docker.compose.service"]: item["State"]
                for item in containers
            }
            migrated = datetime.fromisoformat(states["migrate"]["FinishedAt"])
            for service in ["api", "worker"]:
                assert datetime.fromisoformat(states[service]["StartedAt"]) >= migrated
    finally:
        if watcher is not None and watcher.poll() is None:
            os.killpg(watcher.pid, signal.SIGINT)
            try:
                watcher.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(watcher.pid, signal.SIGKILL)
                watcher.wait(timeout=10)
        stack.run("down", "--volumes", "--remove-orphans")
        subprocess.run(
            [
                "docker",
                "image",
                "rm",
                *[
                    environment["TINYPODS_LOCAL_IMAGE_PREFIX"] + "/" + service + ":local"
                    for service in ["api", "worker", "migrate"]
                ],
            ],
            capture_output=True,
            timeout=30,
        )
