"""Atomic common Valkey limits and HTTP endpoint/global policy composition."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import Request, WebSocket
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from redis.exceptions import ConnectionError
from tinypods_api.cache import CachePolicy
from tinypods_api.http.contracts import LivenessResponse
from tinypods_api.main import create_app
from tinypods_api.rate_limit import EndpointRatePolicy
from tinypods_core.rate_limit import RateLimiter, RateLimitUnavailable, RatePolicy
from tinypods_core.settings import Settings


def configured(url: str) -> Settings:
    """Isolate common limiter keys independently of optional response caching."""
    return Settings(valkey_url=SecretStr(url), rate_limit_namespace="test:limits:" + uuid4().hex)


def test_shared_atomic_reservations_expiry_and_isolation(valkey_url: str) -> None:
    """Enforce exactly one budget across clients, preserve TTL and expire unused subjects."""

    async def scenario() -> None:
        settings = configured(valkey_url)
        one, two = RateLimiter(settings), RateLimiter(settings)
        policy = RatePolicy("uploads", 7, 60_000)
        try:
            results = await asyncio.gather(
                *[(one if index % 2 else two).reserve(policy, "subject") for index in range(20)]
            )
            assert sum(result.allowed for result in results) == 7
            assert all(1 <= result.retry_after <= 60 for result in results)
            key = one.key(policy, "subject")
            assert await one.redis.get(key) == b"7"
            before = await one.redis.pttl(key)
            assert not (await two.reserve(policy, "subject")).allowed
            assert 0 < await one.redis.pttl(key) <= before
            assert (await one.reserve(policy, "another subject")).allowed
            assert (await one.reserve(RatePolicy("downloads", 1, 60_000), "subject")).allowed
            other = RateLimiter(
                settings.model_copy(
                    update={"rate_limit_namespace": settings.rate_limit_namespace + ":other"}
                )
            )
            try:
                assert (await other.reserve(policy, "subject")).allowed
            finally:
                await other.close()
            expiring = RatePolicy("short", 1, 50)
            assert (await one.reserve(expiring, "subject")).allowed
            async with asyncio.timeout(3):
                while await one.redis.exists(one.key(expiring, "subject")):
                    await asyncio.sleep(0.01)
            assert (await two.reserve(expiring, "subject")).allowed
            assert "subject" not in key
            # State without an expiry must fail closed rather than leave an immortal budget.
            await one.redis.set(key, 1)
            with pytest.raises(RateLimitUnavailable):
                await one.reserve(policy, "subject")
        finally:
            await one.close()
            await two.close()

    asyncio.run(scenario())


def test_configuration_deadlines_failures_and_cancellation(
    valkey_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound sockets/pools despite URL options and propagate cancellation/programming errors."""
    with pytest.raises(ValueError):
        RateLimiter(Settings())
    for policy in (
        ("bad name", 1, 1000),
        ("valid", 0, 1000),
        ("valid", 1, 0),
        ("valid", 1_000_001, 1000),
        ("valid", 1, 86_400_001),
    ):
        with pytest.raises(ValueError):
            RatePolicy(*policy)
    with pytest.raises(ValidationError):
        EndpointRatePolicy(name="invalid", limit=0)
    with pytest.raises(ValidationError):
        Settings(rate_limit_valkey_url=SecretStr("http://invalid"))

    async def scenario() -> None:
        limiter = RateLimiter(
            configured(
                valkey_url + "?socket_timeout=60&socket_connect_timeout=60&max_connections=99"
            )
        )
        options = limiter.redis.connection_pool.connection_kwargs
        assert options["socket_timeout"] == options["socket_connect_timeout"] == 0.2
        assert limiter.redis.connection_pool.max_connections == 16
        policy = RatePolicy("test", 1, 1000)
        try:

            async def cancelled(*args: object) -> object:
                """Expose cancellation at the storage boundary."""
                raise asyncio.CancelledError

            monkeypatch.setattr(limiter.redis, "eval", cancelled)
            with pytest.raises(asyncio.CancelledError):
                await limiter.reserve(policy, "subject")

            async def unavailable(*args: object) -> object:
                """Model a transport refusal without allowing a request."""
                raise ConnectionError("sensitive connection details")

            monkeypatch.setattr(limiter.redis, "eval", unavailable)
            with pytest.raises(RateLimitUnavailable) as error:
                await limiter.reserve(policy, "subject")
            assert "sensitive" not in str(error.value)

            async def bug(*args: object) -> object:
                """Expose programming errors rather than translating them as an allowance."""
                raise RuntimeError("bug")

            monkeypatch.setattr(limiter.redis, "eval", bug)
            with pytest.raises(RuntimeError, match="bug"):
                await limiter.reserve(policy, "subject")
        finally:
            await limiter.close()

    asyncio.run(scenario())


def test_endpoint_policy_precedes_cache_and_is_not_auth_specific(valkey_url: str) -> None:
    """Apply a reusable endpoint budget to cached hits, across methods and per transport peer."""
    settings = configured(valkey_url)
    app = create_app(settings)
    calls: list[int] = []

    @app.get(
        "/limited",
        response_model=LivenessResponse,
        operation_id="getLimited",
        openapi_extra={
            **CachePolicy(namespace="limited", revision=1, ttl="metadata").openapi_extra(),
            **EndpointRatePolicy(name="limited", limit=2).openapi_extra(),
        },
    )
    async def limited() -> LivenessResponse:
        """Return public data that shares a reusable non-auth endpoint budget."""
        calls.append(1)
        return LivenessResponse()

    @app.post(
        "/another",
        response_model=LivenessResponse,
        operation_id="postAnother",
        openapi_extra=EndpointRatePolicy(name="another", limit=1).openapi_extra(),
    )
    async def another() -> LivenessResponse:
        """Exercise rate policies independently of caching and authentication."""
        return LivenessResponse()

    @app.get(
        "/no-peer",
        response_model=LivenessResponse,
        operation_id="getNoPeer",
        openapi_extra=EndpointRatePolicy(name="no-peer", limit=1).openapi_extra(),
    )
    async def no_peer(request: Request) -> LivenessResponse:
        """Allow peer-free internal requests to share a bounded fallback bucket."""
        return LivenessResponse()

    with TestClient(app, client=None) as client:
        assert client.get("/limited").headers["x-cache"] == "MISS"
        assert client.get("/limited").headers["x-cache"] == "HIT"
        response = client.get("/limited", headers={"x-forwarded-for": "attacker-controlled"})
        assert response.status_code == 429 and 1 <= int(response.headers["retry-after"]) <= 60
        assert len(calls) == 1
        assert client.post("/another").status_code == 200
        assert client.post("/another").status_code == 429
        assert client.get("/info").status_code == 200
        assert client.get("/no-peer").status_code == 200
    # A fresh app shares the same backend budget, rather than resetting an in-process counter.
    with TestClient(create_app(settings)) as client:
        assert client.get("/info").status_code == 200


def test_global_budget_and_fail_closed_with_probe_exemption(valkey_url: str) -> None:
    """Apply aggregate global limits, keep probes live and reject unavailable configured storage."""
    settings = configured(valkey_url).model_copy(update={"rate_limit_global_per_minute": 2})
    app = create_app(settings)

    @app.websocket("/socket")
    async def websocket(socket: WebSocket) -> None:
        """Exercise non-HTTP passthrough of the global HTTP limiter."""
        await socket.accept()
        await socket.send_text("alive")
        await socket.close()

    with TestClient(app) as client:
        with client.websocket_connect("/socket") as socket:
            assert socket.receive_text() == "alive"
        assert client.get("/info").status_code == 200
        assert client.get("/missing").status_code == 404
        denied = client.get("/info")
        assert denied.status_code == 429 and denied.headers["retry-after"]
        assert denied.json()["request_id"] == denied.headers["x-request-id"]
        assert client.get("/healthz").status_code == client.get("/readyz").status_code == 200
    with TestClient(create_app(settings)) as client:
        assert client.get("/info").status_code == 429
    with TestClient(create_app(Settings(rate_limit_global_per_minute=1))) as client:
        assert client.get("/info").status_code == 503
        assert client.get("/healthz").status_code == 200
    offline = settings.model_copy(
        update={"rate_limit_valkey_url": SecretStr("redis://127.0.0.1:1/0")}
    )
    with TestClient(create_app(offline)) as client:
        assert client.get("/info").status_code == 503
        assert client.get("/healthz").status_code == 200
