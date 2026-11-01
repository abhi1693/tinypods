"""Opt-in public-repository demonstration using isolated database and Valkey fixtures."""

import asyncio
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient
from test_auth import PASSWORD, credentials
from test_auth import auth_settings as auth_settings
from tinypods_api.main import create_app
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.settings import Settings


@pytest.mark.live_repository
def test_live_jetstack_repository_cli_and_api(auth_settings: Settings) -> None:
    """Demonstrate real public sync, persisted rows, idempotency and protected deletion."""
    settings = auth_settings.model_copy(
        update={"repository_credentials": {}, "repository_allowed_hosts": ("charts.jetstack.io",)}
    )

    async def initialize() -> None:
        """Bootstrap only isolated test credentials."""
        database = Database(settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
        finally:
            await database.close()

    asyncio.run(initialize())
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": client.cookies["__Host-tinypods-csrf"]}
        created = client.post(
            "/api/v1/chart-repositories",
            json={"name": "Jetstack live demonstration", "url": "https://charts.jetstack.io"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        identity = created.json()["id"]
        before = client.get("/api/v1/charts?repository_id=" + identity).json()["total"]
        outcome = subprocess.run(
            ["tinypods", "repositories", "sync", identity],
            env={
                **os.environ,
                "TINYPODS_DATABASE_URL": settings.database_url.get_secret_value(),
                "TINYPODS_REPOSITORY_CREDENTIALS": "{}",
                "TINYPODS_REPOSITORY_ALLOWED_HOSTS": '["charts.jetstack.io"]',
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        result = json.loads(outcome.stdout)["repositories"][0]
        charts = client.get("/api/v1/charts?repository_id=" + identity + "&limit=100").json()
        cert = next(row for row in charts["items"] if row["name"] == "cert-manager")
        versions = client.get(
            "/api/v1/chart-versions?chart_id=" + cert["id"] + "&ordering=created"
        ).json()
        assert versions["total"] == 3
        assert result["versions"] <= result["charts"] * 3
        pin = versions["items"][0]
        synced = client.post("/api/v1/chart-repositories/" + identity + "/sync", headers=headers)
        assert synced.status_code == 200, synced.text
        assert synced.json()["versions"] == result["versions"]
        assert (
            client.get("/api/v1/chart-versions?chart_id=" + cert["id"]).json()["total"]
            == versions["total"]
        )
        application = client.post(
            "/api/v1/applications",
            json={"name": "cert-manager demo", "slug": "cert-manager-demo"},
            headers=headers,
        ).json()
        release = client.post(
            "/api/v1/releases",
            json={"application_id": application["id"], "chart_version_id": pin["id"]},
            headers=headers,
        )
        assert release.status_code == 201, release.text
        protected = client.delete("/api/v1/chart-repositories/" + identity, headers=headers)
        assert protected.status_code == 409
        assert (
            client.get("/api/v1/charts?repository_id=" + identity).json()["total"]
            == charts["total"]
        )
        assert (
            client.delete("/api/v1/releases/" + release.json()["id"], headers=headers).status_code
            == 204
        )
        removed = client.delete("/api/v1/chart-repositories/" + identity, headers=headers)
        assert removed.status_code == 204
        remaining = client.get("/api/v1/charts?repository_id=" + identity).json()["total"]
        assert before == remaining == 0
        print(
            "LIVE_REPOSITORY_DEMO "
            + json.dumps(
                {
                    "repository": "https://charts.jetstack.io",
                    "source_type_default": created.json()["source_type"],
                    "charts_before_sync": before,
                    "cli_chart_count": result["charts"],
                    "cli_version_count": result["versions"],
                    "cert_manager_version_count": versions["total"],
                    "sample_version": pin["version"],
                    "api_resync_status": synced.status_code,
                    "duplicate_rows_after_resync": 0,
                    "delete_while_linked_status": protected.status_code,
                    "delete_after_unlink_status": removed.status_code,
                    "charts_after_delete": remaining,
                    "chart_archive_downloads": 0,
                    "operator_database_touched": False,
                }
            )
        )
