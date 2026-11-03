"""Explicit browser-origin configuration for direct cookie-authenticated API calls."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request
from tinypods_api.auth.dependencies import check_origin
from tinypods_api.main import create_app
from tinypods_core.settings import Settings


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.com",
        "ftp://console.example.com",
        "https://",
        "https://user@console.example.com",
        "https://user:pass@console.example.com",
        "https://console.example.com/",
        "https://console.example.com/path",
        "https://console.example.com?x=1",
        "https://console.example.com#fragment",
        "https://console.example.com:99999",
        "http://console.example.com",
        "null",
    ],
)
def test_browser_origins_reject_ambiguous_or_insecure_configuration(origin: str) -> None:
    """Production browser origins must be exact HTTPS origins without credentials."""
    with pytest.raises(ValidationError):
        Settings(browser_origins=[origin])


def test_direct_browser_cors_preflight_is_explicit_and_credentialed() -> None:
    """Allow only configured browser origins and the required CSRF/content headers."""
    settings = Settings(environment="development", browser_origins=["http://console.test:3000"])
    with TestClient(create_app(settings)) as client:
        allowed = client.options(
            "/api/v1/auth/login",
            headers={
                "origin": "http://console.test:3000",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type,x-csrf-token",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://console.test:3000"
        assert allowed.headers["access-control-allow-credentials"] == "true"
        for origin in ("http://foreign.test", "null"):
            denied = client.options(
                "/api/v1/auth/login",
                headers={
                    "origin": origin,
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "content-type,x-csrf-token",
                },
            )
            assert denied.status_code == 400
            assert "access-control-allow-origin" not in denied.headers
        health = client.get("/healthz", headers={"origin": "http://console.test:3000"})
        assert health.status_code == 200
        assert health.headers["access-control-allow-origin"] == "http://console.test:3000"


@pytest.mark.parametrize("origin", [None, "https://api.test", "https://console.test"])
def test_auth_origin_allows_only_same_or_configured_origins(origin: str | None) -> None:
    """Trusted browser origins supplement the API's own origin without skipping CSRF."""
    settings = Settings(browser_origins=["https://console.test"])
    headers = [(b"host", b"api.test")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "method": "POST",
            "path": "/",
            "headers": headers,
            "server": ("api.test", 443),
            "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
        }
    )
    check_origin(request)
    request = Request(
        {**request.scope, "headers": headers[:1] + [(b"origin", b"https://foreign.test")]}
    )
    with pytest.raises(HTTPException) as denied:
        check_origin(request)
    assert denied.value.status_code == 403
