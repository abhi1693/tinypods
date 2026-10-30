"""Disposable PostgreSQL/PgBouncer infrastructure; never use developer databases."""

import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import psycopg
import pytest

POSTGRES = "postgres@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5"
PGBOUNCER = (
    "edoburu/pgbouncer@sha256:8638aa563607256d3c7440437534195f1066a92b68015d89ae9d6b935b444a6d"
)


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class PostgresEndpoints:
    direct: str
    pooled: str


@pytest.fixture(scope="session")
def postgres_endpoints() -> Iterator[PostgresEndpoints]:
    name = "tinypods-tests-" + uuid4().hex[:12]
    password = uuid4().hex
    containers = [name + "-postgres", name + "-pool"]
    docker("network", "create", name)
    try:
        docker(
            "run",
            "-d",
            "--name",
            containers[0],
            "--network",
            name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_USER=tinypods_test",
            "-e",
            "POSTGRES_DB=tinypods_test",
            "-e",
            "POSTGRES_PASSWORD=" + password,
            POSTGRES,
        )
        port = docker("port", containers[0], "5432/tcp").rsplit(":", 1)[1]
        direct = f"postgresql://tinypods_test:{password}@127.0.0.1:{port}/tinypods_test"
        wait_for_postgres(direct)
        docker(
            "run",
            "-d",
            "--name",
            containers[1],
            "--network",
            name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "DB_HOST=" + containers[0],
            "-e",
            "DB_USER=tinypods_test",
            "-e",
            "DB_PASSWORD=" + password,
            "-e",
            "DB_NAME=tinypods_test",
            "-e",
            "POOL_MODE=transaction",
            "-e",
            "AUTH_TYPE=scram-sha-256",
            "-e",
            "DEFAULT_POOL_SIZE=1",
            "-e",
            "MAX_PREPARED_STATEMENTS=0",
            PGBOUNCER,
        )
        port = docker("port", containers[1], "5432/tcp").rsplit(":", 1)[1]
        pooled = f"postgresql://tinypods_test:{password}@127.0.0.1:{port}/tinypods_test"
        wait_for_postgres(pooled)
        yield PostgresEndpoints(direct, pooled)
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=30)


def wait_for_postgres(url: str) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            with psycopg.connect(url, connect_timeout=1):
                return
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                pytest.fail("Disposable PostgreSQL/PgBouncer did not become ready")
            time.sleep(0.1)
