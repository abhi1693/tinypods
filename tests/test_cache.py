"""Exercise async cache storage and HTTP behavior against disposable Valkey."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Annotated, Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import SecretStr, ValidationError
from redis.exceptions import ConnectionError
from tinypods_api.cache import CachedReadRoute, CachePolicy
from tinypods_api.http.contracts import PlatformInfoResponse
from tinypods_api.main import create_app
from tinypods_core import __version__
from tinypods_core.cache import CacheLookup, CacheUnavailable, ResponseCache, json_body
from tinypods_core.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    """Run async Redis tests on the event loop supported by redis.asyncio.

    Returns:
        The asyncio backend name required by the async Redis client.
    """
    return "asyncio"


@pytest.fixture
def cache_settings(valkey_url: str) -> Settings:
    """Build settings with a fresh cache namespace and a small test payload bound.

    Args:
        valkey_url: Ephemeral connection URL supplied by the isolated server fixture.

    Returns:
        Settings for a unique response-cache namespace.
    """
    return Settings(
        valkey_url=SecretStr(valkey_url),
        cache_namespace="tests:" + uuid4().hex,
        cache_max_bytes=1024,
    )


@pytest.fixture
async def cache(cache_settings: Settings) -> AsyncIterator[ResponseCache]:
    """Yield an async cache client and close its owned pool after the test.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Yields:
        Async cache instance whose pool is awaited closed after the test.
    """
    client = ResponseCache(cache_settings)
    try:
        yield client
    finally:
        await client.close()


@asynccontextmanager
async def client_for(
    settings: Settings, application: FastAPI | None = None
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """Start an application lifespan and yield its async HTTP client.

    Args:
        settings: Validated application settings when no application is supplied.
        application: Prepared application with any test-specific routes.

    Yields:
        The application and an in-process HTTP client sharing the test event loop.
    """
    application = application or create_app(settings)
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client,
    ):
        yield application, client


async def test_generation_invalidation_and_lock_ownership(cache: ResponseCache) -> None:
    """Reject stale-generation publishers and prevent deletion of a replacement lock.

    Args:
        cache: Async cache client connected to the disposable Valkey server.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    first = await cache.lookup("public-item")
    assert first.token and first.body is None
    assert await cache.publish(first, b'{"ok":true}', 30)
    assert (await cache.lookup("public-item")).body == b'{"ok":true}'
    await cache.redis.set("rq:job:unrelated", "keep")
    await cache.invalidate()
    assert await cache.redis.get("rq:job:unrelated") == b"keep"
    assert not await cache.publish(first, b'{"stale":true}', 30)
    second = await cache.lookup("public-item")
    assert second.generation != first.generation
    await cache.redis.set(second.lock_key, "another-owner")
    assert not await cache.publish(second, b"{}", 30)
    await cache.release(second)
    assert await cache.redis.get(second.lock_key) == b"another-owner"
    await cache.release(first)


async def test_expiry_body_limits_corruption_and_evicted_generation(cache: ResponseCache) -> None:
    """Recover misses after expiry/corruption without reusing an evicted generation.

    Args:
        cache: Async cache client connected to the disposable Valkey server.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    assert json_body("not-binary", 1024) is None
    first = await cache.lookup("expiry")
    assert await cache.publish(first, b"{}", 1)
    await cache.release(first)
    assert 0 <= await cache.redis.ttl(first.key) <= 1
    await asyncio.sleep(1.05)
    expired = await cache.lookup("expiry")
    assert expired.body is None and expired.token
    assert not await cache.publish(expired, b"x" * 1025, 1)
    with pytest.raises(ValueError, match="positive"):
        await cache.publish(expired, b"{}", 0)
    assert not await cache.publish(replace(expired, token=None), b"{}", 1)
    await cache.redis.set(expired.key, b"not-json")
    assert (await cache.lookup("expiry")).body is None
    await cache.redis.set(expired.key, b"x" * 1025)
    assert (await cache.lookup("expiry")).body is None
    await cache.redis.delete(expired.generation_key)
    replacement = await cache.lookup("expiry")
    assert replacement.generation != expired.generation
    assert not await cache.publish(expired, b"{}", 30)
    await cache.redis.set(expired.generation_key, "invalid")
    with pytest.raises(CacheUnavailable):
        await cache.lookup("expiry")


async def test_breaker_and_pool_bounds(
    cache: ResponseCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open the breaker after one failure and retain bounded binary client settings.

    Args:
        cache: Async cache client connected to the disposable Valkey server.
        monkeypatch: Pytest hook that restores temporary method/configuration substitutions.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    broken = AsyncMock(side_effect=ConnectionError("unavailable"))
    monkeypatch.setattr(cache.redis, "get", broken)
    for _ in range(2):
        with pytest.raises(CacheUnavailable):
            await cache.lookup("failure")
    broken.assert_awaited_once()
    assert cache.redis.connection_pool.max_connections == 32
    options = cache.redis.connection_pool.connection_kwargs
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 0.2
    assert options["decode_responses"] is False


async def test_info_hits_skip_dependencies_and_keep_fresh_request_ids(
    cache_settings: Settings,
) -> None:
    """Return hits before dependencies while retaining per-request correlation IDs.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    dependency = AsyncMock()

    async def count_dependency() -> None:
        """Record whether route dependency resolution ran.

        Returns:
            None. Assertions or recorded calls verify the described behavior.
        """
        await dependency()

    router = APIRouter(route_class=CachedReadRoute)

    @router.get(
        "/items/{item_id}",
        response_model=PlatformInfoResponse,
        dependencies=[Depends(count_dependency)],
        openapi_extra=CachePolicy(namespace="items", revision=1, ttl="content").openapi_extra(),
    )
    async def item(item_id: str) -> PlatformInfoResponse:
        """Return the requested item identifier as the public test representation.

        Args:
            item_id: Path identifier encoded into the public test response.

        Returns:
            Public DTO with the item identifier as its version value.
        """
        return PlatformInfoResponse(version=item_id)

    app.include_router(router)
    async with client_for(cache_settings, app) as (_, client):
        first, second = await client.get("/info"), await client.get("/info")
        assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
        assert first.headers["x-request-id"] != second.headers["x-request-id"]
        assert second.json() == {"service": "TinyPods", "version": __version__}
        assert second.headers["cache-control"] == "no-store"
        assert (await client.get("/items/one")).headers["x-cache"] == "MISS"
        assert (await client.get("/items/one")).headers["x-cache"] == "HIT"
        dependency.assert_awaited_once()
        assert (await client.get("/items/two")).json()["version"] == "two"
        assert "x-cache" not in (await client.get("/healthz")).headers
        assert "x-cache" not in (await client.get("/readyz")).headers


@pytest.mark.parametrize(
    "headers,query,reason",
    [
        ({"Authorization": "Bearer one"}, "", "authorization"),
        ({"Cookie": "session=one"}, "", "cookie"),
        ({"Cache-Control": "no-cache"}, "", "request_cache_control"),
        ({"Cache-Control": "no-store"}, "", "request_cache_control"),
        ({}, "?q=" + "x" * 4100, "query_too_long"),
    ],
)
async def test_sensitive_or_explicit_requests_bypass(
    cache_settings: Settings, headers: dict[str, str], query: str, reason: str
) -> None:
    """Read through credentials and explicit bypass requests without creating hits.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.
        headers: Request headers selecting the parameterized bypass case.
        query: Query suffix for the parameterized HTTP request.
        reason: Expected fixed cache-bypass reason.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    async with client_for(cache_settings) as (_, client):
        for _ in range(2):
            response = await client.get("/info" + query, headers=headers)
            assert response.status_code == 200
            assert response.headers["x-cache"] == "BYPASS"
            assert response.headers["x-cache-bypass-reason"] == reason


@pytest.mark.parametrize("mode", ["disabled", "unconfigured", "offline"])
async def test_optional_cache_failure_reads_through(mode: str) -> None:
    """Keep public reads available when caching is disabled, absent or unreachable.

    Args:
        mode: Response/configuration variant selected by the parameterized test.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    settings = Settings(
        cache_enabled=mode != "disabled",
        valkey_url=SecretStr("redis://127.0.0.1:1/0") if mode != "unconfigured" else None,
    )
    async with client_for(settings) as (_, client):
        response = await client.get("/info")
        assert response.status_code == 200 and response.headers["x-cache"] == "BYPASS"
        assert response.headers["x-cache-bypass-reason"] == (
            "cache_unavailable" if mode == "offline" else "disabled"
        )


async def test_keys_preserve_query_semantics_and_database_identity(
    cache_settings: Settings,
) -> None:
    """Canonicalize query names without merging repeated-value order or database scopes.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)

    @app.get(
        "/query",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="query", revision=1, ttl="content").openapi_extra(),
    )
    async def query_endpoint(
        a: Annotated[list[str] | None, Query()] = None, b: str | None = None
    ) -> PlatformInfoResponse:
        """Return the parsed query values so cache identity can be checked.

        Args:
            a: Ordered repeated values bound from query parameter a.
            b: Optional query parameter b used to test canonical name ordering.

        Returns:
            Public DTO encoding the parsed query values.
        """
        return PlatformInfoResponse(version=json.dumps([a, b]))

    async with client_for(cache_settings, app) as (_, client):
        for query, expected in [
            ("?a=1&b=2", "MISS"),
            ("?b=2&a=1", "HIT"),
            ("?a=1&a=2", "MISS"),
            ("?a=2&a=1", "MISS"),
            ("?a=1&a=2", "HIT"),
        ]:
            assert (await client.get("/query" + query)).headers["x-cache"] == expected
    one = ResponseCache(cache_settings)
    other = ResponseCache(
        cache_settings.model_copy(
            update={"database_url": SecretStr("postgresql://another-database")}
        )
    )
    try:
        assert one.namespace != other.namespace and __version__ in one.namespace
    finally:
        await one.close()
        await other.close()


@pytest.mark.parametrize(
    "mode", ["cookie", "vary", "cache_control", "no_store", "custom", "stream", "error", "text"]
)
async def test_unsafe_responses_are_not_stored(cache_settings: Settings, mode: str) -> None:
    """Never reuse responses with unsafe headers, errors, streams or non-JSON bodies.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.
        mode: Response/configuration variant selected by the parameterized test.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/unsafe",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="unsafe", revision=1, ttl="content").openapi_extra(),
    )
    async def unsafe() -> JSONResponse | StreamingResponse | PlainTextResponse:
        """Return the selected response variant to test cache eligibility.

        Returns:
            Selected non-cacheable HTTP response; the error variant raises HTTPException.
        """
        nonlocal calls
        calls += 1
        if mode == "error":
            raise HTTPException(403, "not allowed")
        if mode == "text":
            return PlainTextResponse("plain")
        if mode == "stream":

            async def chunks() -> AsyncIterator[bytes]:
                """Yield a JSON chunk through the streaming response path."""
                yield b"{}"

            return StreamingResponse(chunks(), media_type="application/json")
        headers = {
            "cookie": {"Set-Cookie": "session=one"},
            "vary": {"Vary": "Accept"},
            "cache_control": {"Cache-Control": "private"},
            "no_store": {"Cache-Control": "no-store"},
            "custom": {"X-Personalized": "true"},
        }[mode]
        return JSONResponse({}, headers=headers)

    async with client_for(cache_settings, app) as (_, client):
        for _ in range(2):
            assert (await client.get("/unsafe")).headers.get("x-cache") != "HIT"
    assert calls == 2


async def test_stampede_waits_for_owner(cache_settings: Settings) -> None:
    """Coalesce concurrent misses into one render and one subsequent cache hit.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/slow",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="slow", revision=1, ttl="content").openapi_extra(),
    )
    async def slow() -> PlatformInfoResponse:
        """Delay the initial render long enough for a second request to wait.

        Returns:
            Public DTO after an async render delay.
        """
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.15)
        return PlatformInfoResponse(version=__version__)

    async with client_for(cache_settings, app) as (_, client):
        first, second = await asyncio.gather(client.get("/slow"), client.get("/slow"))
        assert {first.headers["x-cache"], second.headers["x-cache"]} == {"MISS", "HIT"}
    assert calls == 1


async def test_async_lookup_keeps_event_loop_responsive(
    cache_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow another task to advance while an awaited cache lookup is delayed.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.
        monkeypatch: Pytest hook that restores temporary method/configuration substitutions.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    cache: ResponseCache = app.state.response_cache
    original = cache.lookup

    async def delayed(key: str) -> CacheLookup:
        """Simulate slow async transport before performing the actual lookup.

        Args:
            key: Canonical response identity passed to the original lookup.

        Returns:
            The original lookup result after the simulated transport delay.
        """
        await asyncio.sleep(0.2)
        return await original(key)

    monkeypatch.setattr(cache, "lookup", delayed)
    async with client_for(cache_settings, app) as (_, client):
        task = asyncio.create_task(client.get("/info"))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert (await task).status_code == 200


def test_policy_rejects_non_get_caching() -> None:
    """Reject explicit write policies and invalid Valkey URLs during configuration.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    policy = CachePolicy(namespace="invalid", revision=1, ttl="content")
    for method in ["POST", "PUT", "DELETE"]:
        app = create_app(Settings())
        with pytest.raises(ValueError, match="Only public GET"):
            app.add_api_route(
                "/write", lambda: {}, methods=[method], openapi_extra=policy.openapi_extra()
            )
    for value in ["https://cache", "redis://", "redis://host:invalid"]:
        with pytest.raises(ValidationError):
            Settings(cache_valkey_url=SecretStr(value))
    with pytest.raises(ValueError, match="Valkey URL"):
        ResponseCache(Settings())


async def test_cached_json_is_validated_and_extra_fields_are_not_replayed(
    cache_settings: Settings,
) -> None:
    """Recompute invalid DTOs and filter undeclared fields from otherwise valid hits.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    cache: ResponseCache = app.state.response_cache
    async with client_for(cache_settings, app) as (_, client):
        assert (await client.get("/info")).headers["x-cache"] == "MISS"
        keys = [
            key
            async for key in cache.redis.scan_iter(match=f"{cache.namespace}:public:*")
            if not key.endswith((b":lock", b":generation"))
        ]
        assert len(keys) == 1
        await cache.redis.set(keys[0], b'{"private":"not-a-platform-response"}', ex=30)
        response = await client.get("/info")
        assert response.headers["x-cache-bypass-reason"] == "invalid_entry"
        assert response.json()["version"] == __version__
        assert b"not-a-platform-response" not in response.content
        assert await cache.redis.get(keys[0]) is None
        await client.get("/info")
        await cache.redis.set(
            keys[0],
            json.dumps(
                {"service": "TinyPods", "version": __version__, "password": "do-not-replay"}
            ),
            ex=30,
        )
        response = await client.get("/info")
        assert response.headers["x-cache"] == "HIT" and "password" not in response.json()


async def test_cache_keys_and_failure_logs_do_not_expose_credentials(
    cache: ResponseCache, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash request identities and omit Redis connection error text from warnings.

    Args:
        cache: Async cache client connected to the disposable Valkey server.
        capsys: Captured stdout/stderr used to check that warning text excludes credentials.
        monkeypatch: Pytest hook that restores temporary method/configuration substitutions.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    lookup = await cache.lookup("private-query-value")
    assert "private-query-value" not in lookup.key and "redis://" not in lookup.key
    assert await cache.publish(lookup, b"{}", 30)
    monkeypatch.setattr(
        cache.redis,
        "get",
        AsyncMock(side_effect=ConnectionError("redis://user:private-password@host")),
    )
    with pytest.raises(CacheUnavailable):
        await cache.lookup("other")
    assert "private-password" not in capsys.readouterr().err


async def test_lock_expiry_and_invalid_json_are_safe(cache: ResponseCache) -> None:
    """Reject expired ownership and treat non-finite cached JSON as a miss.

    Args:
        cache: Async cache client connected to the disposable Valkey server.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    original = await cache.lookup("locked")
    await cache.redis.delete(original.lock_key)
    replacement = await cache.lookup("locked")
    assert replacement.token is not None and replacement.token != original.token
    assert not await cache.publish(original, b"{}", 30)
    await cache.release(original)
    assert await cache.redis.get(replacement.lock_key) == replacement.token.encode()
    assert await cache.publish(replacement, b"{}", 30)
    await cache.redis.set(replacement.key, b'{"number": NaN}')
    assert (await cache.lookup("locked")).body is None


@pytest.mark.parametrize("failure", ["publish", "release"])
async def test_optional_write_and_cleanup_failures_do_not_fail_reads(
    cache_settings: Settings, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Return the rendered body even if optional publication or lock cleanup fails.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.
        monkeypatch: Pytest hook that restores temporary method/configuration substitutions.
        failure: Optional storage method whose awaited call is made unavailable.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    broken = AsyncMock(side_effect=CacheUnavailable)
    monkeypatch.setattr(app.state.response_cache, failure, broken)
    async with client_for(cache_settings, app) as (_, client):
        response = await client.get("/info")
        assert response.status_code == 200 and response.headers["x-cache"] == "MISS"
    broken.assert_awaited()


async def test_invalidation_during_fill_cannot_repopulate_old_generation(
    cache_settings: Settings,
) -> None:
    """Rotate the generation while a render waits and reject its late publication.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    cache: ResponseCache = app.state.response_cache
    started, proceed = asyncio.Event(), asyncio.Event()
    calls = 0

    @app.get(
        "/racing",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="racing", revision=1, ttl="content").openapi_extra(),
    )
    async def racing() -> PlatformInfoResponse:
        """Pause the first render until the test has invalidated its generation.

        Returns:
            Public DTO containing the render count.
        """
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await proceed.wait()
        return PlatformInfoResponse(version=str(calls))

    async with client_for(cache_settings, app) as (_, client):
        task = asyncio.create_task(client.get("/racing"))
        await started.wait()
        await cache.invalidate()
        proceed.set()
        assert (await task).status_code == 200
        response = await client.get("/racing")
        assert response.headers["x-cache"] == "MISS" and response.json()["version"] == "2"
        assert (await client.get("/racing")).headers["x-cache"] == "HIT"


async def test_cache_wait_deadline_is_bounded(
    cache_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render without ownership after the bounded wait instead of waiting indefinitely.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.
        monkeypatch: Pytest hook that restores temporary method/configuration substitutions.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    from tinypods_api.cache import routes as route_cache

    monkeypatch.setattr(route_cache, "CACHE_WAIT_SECONDS", 0.01)
    app = create_app(cache_settings)
    cache: ResponseCache = app.state.response_cache
    original = cache.lookup

    async def contested(identity: str) -> CacheLookup:
        """Simulate a miss owned by another loader.

        Args:
            identity: Canonical response identity for a simulated contested lookup.

        Returns:
            Lookup state with publication ownership removed.
        """
        return replace(await original(identity), token=None)

    monkeypatch.setattr(cache, "lookup", contested)
    async with client_for(cache_settings, app) as (_, client):
        assert (await client.get("/info")).headers["x-cache"] == "MISS"


async def test_response_size_limit_prevents_storage(cache_settings: Settings) -> None:
    """Return oversized source responses without reusing them as stored cache hits.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/large",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="large", revision=1, ttl="content").openapi_extra(),
    )
    async def large() -> PlatformInfoResponse:
        """Produce a body above the test cache's storage limit.

        Returns:
            DTO whose serialized body exceeds the test cache's storage limit.
        """
        nonlocal calls
        calls += 1
        return PlatformInfoResponse(version="x" * 1100)

    async with client_for(cache_settings, app) as (_, client):
        assert (await client.get("/large")).headers["x-cache"] == "MISS"
        assert (await client.get("/large")).headers["x-cache"] == "MISS"
    assert calls == 2


async def test_cache_url_can_be_separate_and_io_overrides_remain_bounded(
    cache_settings: Settings,
) -> None:
    """Retain timeout and binary response limits despite URL query overrides.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    url = cache_settings.valkey_url.get_secret_value() + "?socket_timeout=20&decode_responses=true"
    cache = ResponseCache(cache_settings.model_copy(update={"cache_valkey_url": SecretStr(url)}))
    try:
        assert (await cache.lookup("binary")).body is None
        assert cache.redis.connection_pool.connection_kwargs["socket_timeout"] == 0.2
        assert cache.redis.connection_pool.connection_kwargs["decode_responses"] is False
    finally:
        await cache.close()


async def test_cancelled_fill_releases_its_lock(cache_settings: Settings) -> None:
    """Await lock cleanup when cancellation interrupts a render that owns a fill token.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    started = asyncio.Event()

    @app.get(
        "/cancelled",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="cancelled", revision=1, ttl="content").openapi_extra(),
    )
    async def cancelled() -> PlatformInfoResponse:
        """Keep the render suspended until its request task is cancelled.

        Returns:
            A DTO only if the deliberately suspended request were resumed.
        """
        started.set()
        await asyncio.Event().wait()
        return PlatformInfoResponse(version=__version__)

    async with client_for(cache_settings, app) as (_, client):
        task = asyncio.create_task(client.get("/cancelled"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cache: ResponseCache = app.state.response_cache
        assert not [
            key async for key in cache.redis.scan_iter(match=f"{cache.namespace}:public:*:lock")
        ]


async def test_real_read_timeout_is_bounded_and_does_not_fail_http(
    cache_settings: Settings,
) -> None:
    """Keep the event loop responsive and read through an actual paused Valkey socket.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    async with client_for(cache_settings) as (app, client):
        cache: ResponseCache = app.state.response_cache
        await cache.redis.ping()
        await cache.redis.execute_command("CLIENT", "PAUSE", 350, "ALL")
        started = time.monotonic()
        task = asyncio.create_task(client.get("/info"))
        await asyncio.sleep(0.05)
        assert not task.done()
        response = await task
        assert response.status_code == 200
        assert response.headers["x-cache-bypass-reason"] == "cache_unavailable"
        assert time.monotonic() - started < 2
        await asyncio.sleep(0.4)


async def test_ignored_query_variations_do_not_allocate_cache_entries(
    cache_settings: Settings,
) -> None:
    """Bypass ignored query parameters before any Valkey allocation.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    async with client_for(cache_settings) as (app, client):
        for value in range(10):
            response = await client.get(f"/info?ignored={value}")
            assert response.headers["x-cache-bypass-reason"] == "undeclared_query"
        cache: ResponseCache = app.state.response_cache
        assert not [key async for key in cache.redis.scan_iter(match=f"{cache.namespace}:public:*")]


async def test_cache_policy_is_opt_in_independent_of_route_names(cache_settings: Settings) -> None:
    """Run unannotated handlers live and apply explicit policies without a name blacklist.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    app = create_app(cache_settings)
    calls = 0

    @app.get("/another-probe", response_model=PlatformInfoResponse)
    async def another_probe() -> PlatformInfoResponse:
        """Return an incrementing value that proves the handler ran again.

        Returns:
            DTO containing an incremented call count.
        """
        nonlocal calls
        calls += 1
        return PlatformInfoResponse(version=str(calls))

    router = APIRouter(route_class=CachedReadRoute)

    @router.get(
        "/healthz",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="explicit", revision=1, ttl="content").openapi_extra(),
    )
    async def explicitly_selected() -> PlatformInfoResponse:
        """Return a static body from a deliberately policy-annotated test route.

        Returns:
            Static public DTO for the policy-annotated test route.
        """
        return PlatformInfoResponse(version=__version__)

    app.include_router(router, prefix="/explicit")
    async with client_for(cache_settings, app) as (_, client):
        first, second = await client.get("/another-probe"), await client.get("/another-probe")
        assert first.json()["version"] != second.json()["version"]
        assert "x-cache" not in first.headers and "x-cache" not in second.headers
        assert (await client.get("/explicit/healthz")).headers["x-cache"] == "MISS"
        assert (await client.get("/explicit/healthz")).headers["x-cache"] == "HIT"
        assert "x-cache" not in (await client.get("/healthz")).headers
        assert "x-cache" not in (await client.get("/readyz")).headers


async def test_unannotated_readiness_rechecks_dependencies(cache_settings: Settings) -> None:
    """Observe a changing dependency on consecutive uncached readiness requests.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    calls = 0

    async def dependency() -> bool:
        """Pass only the first readiness evaluation.

        Returns:
            True on the first readiness call and False thereafter.
        """
        nonlocal calls
        calls += 1
        return calls == 1

    app = create_app(cache_settings, readiness_checks={"changing": dependency})
    async with client_for(cache_settings, app) as (_, client):
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
    assert calls == 2


async def test_lifespan_awaits_cache_pool_shutdown(
    cache_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the cache pool through lifespan even when another resource's cleanup fails.

    Args:
        cache_settings: Validated settings with a fresh Valkey namespace and bounded test payloads.
        monkeypatch: Pytest hook that restores temporary method/configuration substitutions.

    Returns:
        None. Assertions or recorded calls verify the described behavior.
    """
    from tinypods_api import main as api_main

    database = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        close=AsyncMock(side_effect=RuntimeError("database shutdown")),
    )

    def database_for_test(settings: Settings) -> SimpleNamespace:
        """Return the prepared database whose close operation raises.

        Args:
            settings: Factory configuration accepted without opening a real database.

        Returns:
            The controlled asynchronous database double.
        """
        return database

    monkeypatch.setattr(api_main, "Database", database_for_test)
    app = create_app(
        cache_settings.model_copy(update={"database_url": SecretStr("postgresql://unused")})
    )
    cache: ResponseCache = app.state.response_cache
    close = AsyncMock(wraps=cache.close)
    monkeypatch.setattr(cache, "close", close)
    with pytest.raises(RuntimeError, match="database shutdown"):
        async with client_for(cache_settings, app) as (_, client):
            assert (await client.get("/info")).status_code == 200
    close.assert_awaited_once()


async def test_invalid_hit_does_not_retry_a_failing_source(cache_settings: Settings) -> None:
    """Propagate a source failure once rather than catching it as a cache outage.

    Args:
        cache_settings: Validated settings with an isolated Valkey namespace.

    Returns:
        None. A corrupted hit triggers one render and one HTTP 500 response.
    """
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/source-error",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(
            namespace="source_error", revision=1, ttl="content"
        ).openapi_extra(),
    )
    async def source_error() -> PlatformInfoResponse:
        """Return one seed body, then raise a distinguishable source-layer failure.

        Returns:
            A public DTO on the initial call.

        Raises:
            CacheUnavailable: Later calls simulate an exception originating in the endpoint.
        """
        nonlocal calls
        calls += 1
        if calls > 1:
            raise CacheUnavailable
        return PlatformInfoResponse(version=__version__)

    # Surface the generated HTTP error instead of re-raising it into the test client.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        assert (await client.get("/source-error")).headers["x-cache"] == "MISS"
        cache: ResponseCache = app.state.response_cache
        keys = [
            key
            async for key in cache.redis.scan_iter(match=f"{cache.namespace}:public:*")
            if not key.endswith((b":lock", b":generation"))
        ]
        await cache.redis.set(keys[0], b'{"invalid":"dto"}', ex=30)
        assert (await client.get("/source-error")).status_code == 500
    assert calls == 2
