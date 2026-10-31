"""Optional cache correctness and isolation against disposable Valkey."""

import asyncio
import json
import time
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from redis.exceptions import ConnectionError
from tinypods_api.cache import CachedReadRoute, CachePolicy
from tinypods_api.contracts import PlatformInfoResponse
from tinypods_api.main import create_app
from tinypods_core import __version__
from tinypods_core.cache import CacheUnavailable, ResponseCache
from tinypods_core.settings import Settings


@pytest.fixture
def cache_settings(valkey_url):
    return Settings(
        valkey_url=SecretStr(valkey_url),
        cache_namespace="tests:" + uuid4().hex,
        cache_max_bytes=1024,
    )


@pytest.fixture
def cache(cache_settings):
    result = ResponseCache(cache_settings)
    try:
        yield result
    finally:
        result.close()


def test_generation_invalidation_and_lock_ownership(cache):
    first = cache.lookup("public-item")
    assert first.token and first.body is None
    assert cache.publish(first, b'{"ok":true}', 30)
    assert cache.lookup("public-item").body == b'{"ok":true}'
    cache.redis.set("rq:job:unrelated", "keep")
    cache.invalidate()
    assert cache.redis.get("rq:job:unrelated") == b"keep"
    assert not cache.publish(first, b'{"stale":true}', 30)
    second = cache.lookup("public-item")
    assert second.generation != first.generation
    cache.redis.set(second.lock_key, "another-owner")
    assert not cache.publish(second, b"{}", 30)
    cache.release(second)
    assert cache.redis.get(second.lock_key) == b"another-owner"
    cache.release(first)


def test_expiry_body_limits_corruption_and_evicted_generation(cache):
    first = cache.lookup("expiry")
    assert cache.publish(first, b"{}", 1)
    cache.release(first)
    assert 0 <= cache.redis.ttl(first.key) <= 1
    time.sleep(1.05)
    expired = cache.lookup("expiry")
    assert expired.body is None and expired.token
    assert not cache.publish(expired, b"x" * 1025, 1)
    with pytest.raises(ValueError, match="positive"):
        cache.publish(expired, b"{}", 0)
    assert not cache.publish(replace(expired, token=None), b"{}", 1)
    cache.redis.set(expired.key, b"not-json")
    assert cache.lookup("expiry").body is None
    cache.redis.set(expired.key, b"x" * 1025)
    assert cache.lookup("expiry").body is None
    cache.redis.delete(expired.generation_key)
    next_lookup = cache.lookup("expiry")
    assert next_lookup.generation != expired.generation
    assert not cache.publish(expired, b"{}", 30)
    cache.redis.set(expired.generation_key, "invalid")
    with pytest.raises(CacheUnavailable):
        cache.lookup("expiry")


def test_breaker_and_pool_bounds(cache, monkeypatch):
    calls = 0

    def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ConnectionError("unavailable")

    monkeypatch.setattr(cache.redis, "get", broken)
    for _ in range(2):
        with pytest.raises(CacheUnavailable):
            cache.lookup("failure")
    assert calls == 1
    assert cache.redis.connection_pool.max_connections == 32
    options = cache.redis.connection_pool.connection_kwargs
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 0.2
    assert options["decode_responses"] is False


def test_info_hits_skip_dependencies_and_keep_fresh_request_ids(cache_settings):
    app = create_app(cache_settings)
    calls = 0

    async def dependency():
        nonlocal calls
        calls += 1

    router = APIRouter(route_class=CachedReadRoute)

    @router.get(
        "/items/{item_id}",
        response_model=PlatformInfoResponse,
        dependencies=[Depends(dependency)],
        openapi_extra=CachePolicy(namespace="items", revision=1, ttl="content").openapi_extra(),
    )
    async def item(item_id: str) -> PlatformInfoResponse:
        """Return HTTP 200 with a public item's version."""
        return PlatformInfoResponse(version=item_id)

    app.include_router(router)
    with TestClient(app) as client:
        first, second = client.get("/info"), client.get("/info")
        assert first.headers["x-cache"] == "MISS"
        assert second.headers["x-cache"] == "HIT"
        assert first.headers["x-request-id"] != second.headers["x-request-id"]
        assert second.json() == {"service": "TinyPods", "version": __version__}
        assert second.headers["cache-control"] == "no-store"
        assert client.get("/items/one").headers["x-cache"] == "MISS"
        assert client.get("/items/one").headers["x-cache"] == "HIT"
        assert calls == 1
        assert client.get("/items/two").json()["version"] == "two"
        assert "x-cache" not in client.get("/healthz").headers
        assert "x-cache" not in client.get("/readyz").headers


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
def test_sensitive_or_explicit_requests_bypass(cache_settings, headers, query, reason):
    with TestClient(create_app(cache_settings)) as client:
        for _ in range(2):
            response = client.get("/info" + query, headers=headers)
            assert response.status_code == 200
            assert response.headers["x-cache"] == "BYPASS"
            assert response.headers["x-cache-bypass-reason"] == reason


@pytest.mark.parametrize("mode", ["disabled", "unconfigured", "offline"])
def test_optional_cache_failure_reads_through(mode):
    settings = Settings(
        cache_enabled=mode != "disabled",
        valkey_url=SecretStr("redis://127.0.0.1:1/0") if mode != "unconfigured" else None,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/info")
        assert response.status_code == 200
        assert response.headers["x-cache"] == "BYPASS"
        assert response.headers["x-cache-bypass-reason"] == (
            "cache_unavailable" if mode == "offline" else "disabled"
        )


def test_keys_preserve_duplicate_order_and_separate_paths_revisions(cache_settings):
    with TestClient(create_app(cache_settings)) as client:
        for query, expected in [
            ("?a=1&b=2", "MISS"),
            ("?b=2&a=1", "HIT"),
            ("?a=1&a=2", "MISS"),
            ("?a=2&a=1", "MISS"),
            ("?a=1&a=2", "HIT"),
        ]:
            assert client.get("/info" + query).headers["x-cache"] == expected
    one = ResponseCache(cache_settings)
    other = ResponseCache(
        cache_settings.model_copy(
            update={"database_url": SecretStr("postgresql://another-database")}
        )
    )
    try:
        assert one.namespace != other.namespace
        assert __version__ in one.namespace
    finally:
        one.close()
        other.close()


@pytest.mark.parametrize(
    "mode", ["cookie", "vary", "cache_control", "no_store", "custom", "stream", "error", "text"]
)
def test_unsafe_responses_are_not_stored(cache_settings, mode):
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/unsafe",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="unsafe", revision=1, ttl="content").openapi_extra(),
    )
    async def unsafe():
        nonlocal calls
        calls += 1
        if mode == "error":
            raise HTTPException(403, "not allowed")
        if mode == "text":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse("plain")
        if mode == "stream":
            return StreamingResponse(iter([b"{}"]), media_type="application/json")
        headers = {
            "cookie": {"Set-Cookie": "session=one"},
            "vary": {"Vary": "Accept"},
            "cache_control": {"Cache-Control": "private"},
            "no_store": {"Cache-Control": "no-store"},
            "custom": {"X-Personalized": "true"},
        }[mode]
        return JSONResponse({}, headers=headers)

    with TestClient(app) as client:
        for _ in range(2):
            response = client.get("/unsafe")
            assert response.headers.get("x-cache") != "HIT"
    assert calls == 2


def test_stampede_waits_for_owner_without_blocking_loop(cache_settings):
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/slow",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="slow", revision=1, ttl="content").openapi_extra(),
    )
    async def slow() -> PlatformInfoResponse:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.15)
        return PlatformInfoResponse(version=__version__)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first, second = await asyncio.gather(client.get("/slow"), client.get("/slow"))
            assert {first.headers["x-cache"], second.headers["x-cache"]} == {"MISS", "HIT"}
        app.state.response_cache.close()

    asyncio.run(scenario())
    assert calls == 1


def test_blocking_client_is_offloaded(cache_settings, monkeypatch):
    app = create_app(cache_settings)
    cache = app.state.response_cache
    original = cache.lookup

    def delayed(key):
        time.sleep(0.2)
        return original(key)

    monkeypatch.setattr(cache, "lookup", delayed)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(client.get("/info"))
            await asyncio.sleep(0.05)
            assert not task.done()
            assert (await task).status_code == 200
        cache.close()

    asyncio.run(scenario())


def test_policy_rejects_probe_and_write_caching():
    policy = CachePolicy(namespace="invalid", revision=1, ttl="content")
    for path, method in [("/healthz", "GET"), ("/readyz", "GET"), ("/write", "POST")]:
        app = create_app(Settings())
        with pytest.raises(ValueError, match="Only public GET"):
            app.add_api_route(
                path, lambda: {}, methods=[method], openapi_extra=policy.openapi_extra()
            )
    for value in ["https://cache", "redis://", "redis://host:invalid"]:
        with pytest.raises(ValidationError):
            Settings(cache_valkey_url=SecretStr(value))
    with pytest.raises(ValueError, match="Valkey URL"):
        ResponseCache(Settings())


def test_cached_json_is_validated_and_extra_fields_are_not_replayed(cache_settings):
    app = create_app(cache_settings)
    cache = app.state.response_cache
    with TestClient(app) as client:
        assert client.get("/info").headers["x-cache"] == "MISS"
        keys = [
            key
            for key in cache.redis.scan_iter(match=f"{cache.namespace}:public:*")
            if not key.endswith((b":lock", b":generation"))
        ]
        assert len(keys) == 1
        cache.redis.set(keys[0], b'{"private":"not-a-platform-response"}', ex=30)
        response = client.get("/info")
        assert response.headers["x-cache-bypass-reason"] == "invalid_entry"
        assert response.json()["version"] == __version__
        assert b"not-a-platform-response" not in response.content
        assert cache.redis.get(keys[0]) is None
        client.get("/info")
        cache.redis.set(
            keys[0],
            json.dumps(
                {"service": "TinyPods", "version": __version__, "password": "do-not-replay"}
            ),
            ex=30,
        )
        response = client.get("/info")
        assert response.headers["x-cache"] == "HIT"
        assert "password" not in response.json()


def test_cache_keys_and_failure_logs_do_not_expose_credentials(cache, capsys, monkeypatch):
    lookup = cache.lookup("private-query-value")
    assert "private-query-value" not in lookup.key
    assert "redis://" not in lookup.key
    assert cache.publish(lookup, b"{}", 30)

    def broken(*args, **kwargs):
        raise ConnectionError("redis://user:private-password@host")

    monkeypatch.setattr(cache.redis, "get", broken)
    with pytest.raises(CacheUnavailable):
        cache.lookup("other")
    assert "private-password" not in capsys.readouterr().err


def test_lock_expiry_and_invalid_json_are_safe(cache):
    original = cache.lookup("locked")
    cache.redis.delete(original.lock_key)
    replacement = cache.lookup("locked")
    assert replacement.token != original.token
    assert not cache.publish(original, b"{}", 30)
    cache.release(original)
    assert cache.redis.get(replacement.lock_key) == replacement.token.encode()
    assert cache.publish(replacement, b"{}", 30)
    cache.redis.set(replacement.key, b'{"number": NaN}')
    assert cache.lookup("locked").body is None


@pytest.mark.parametrize("failure", ["publish", "release"])
def test_optional_write_and_cleanup_failures_do_not_fail_reads(
    cache_settings, monkeypatch, failure
):
    app = create_app(cache_settings)

    def broken(*args, **kwargs):
        raise CacheUnavailable

    monkeypatch.setattr(app.state.response_cache, failure, broken)
    with TestClient(app) as client:
        response = client.get("/info")
        assert response.status_code == 200
        assert response.headers["x-cache"] == "MISS"


def test_invalidation_during_fill_cannot_repopulate_old_generation(cache_settings):
    app = create_app(cache_settings)
    cache = app.state.response_cache
    started = asyncio.Event()
    proceed = asyncio.Event()
    calls = 0

    @app.get(
        "/racing",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="racing", revision=1, ttl="content").openapi_extra(),
    )
    async def racing() -> PlatformInfoResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await proceed.wait()
        return PlatformInfoResponse(version=str(calls))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(client.get("/racing"))
            await started.wait()
            await asyncio.to_thread(cache.invalidate)
            proceed.set()
            assert (await task).status_code == 200
            response = await client.get("/racing")
            assert response.headers["x-cache"] == "MISS"
            assert response.json()["version"] == "2"
            assert (await client.get("/racing")).headers["x-cache"] == "HIT"
        cache.close()

    asyncio.run(scenario())


def test_cache_wait_deadline_is_bounded(cache_settings, monkeypatch):
    from tinypods_api import cache as route_cache

    monkeypatch.setattr(route_cache, "WAIT_SECONDS", 0.01)
    app = create_app(cache_settings)
    cache = app.state.response_cache
    original = cache.lookup

    def contested(identity):
        lookup = original(identity)
        return replace(lookup, token=None)

    monkeypatch.setattr(cache, "lookup", contested)
    with TestClient(app) as client:
        assert client.get("/info").headers["x-cache"] == "MISS"


def test_response_size_limit_prevents_storage(cache_settings):
    app = create_app(cache_settings)
    calls = 0

    @app.get(
        "/large",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="large", revision=1, ttl="content").openapi_extra(),
    )
    async def large() -> PlatformInfoResponse:
        nonlocal calls
        calls += 1
        return PlatformInfoResponse(version="x" * 1100)

    with TestClient(app) as client:
        assert client.get("/large").headers["x-cache"] == "MISS"
        assert client.get("/large").headers["x-cache"] == "MISS"
    assert calls == 2


def test_cache_url_can_be_separate_and_io_overrides_remain_bounded(cache_settings):
    url = cache_settings.valkey_url.get_secret_value() + "?socket_timeout=20&decode_responses=true"
    settings = cache_settings.model_copy(update={"cache_valkey_url": SecretStr(url)})
    cache = ResponseCache(settings)
    try:
        assert cache.lookup("binary").body is None
        assert cache.redis.connection_pool.connection_kwargs["socket_timeout"] == 0.2
        assert cache.redis.connection_pool.connection_kwargs["decode_responses"] is False
    finally:
        cache.close()


def test_cancelled_fill_releases_its_lock(cache_settings):
    app = create_app(cache_settings)
    started = asyncio.Event()

    @app.get(
        "/cancelled",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(namespace="cancelled", revision=1, ttl="content").openapi_extra(),
    )
    async def cancelled() -> PlatformInfoResponse:
        started.set()
        await asyncio.Event().wait()
        return PlatformInfoResponse(version=__version__)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(client.get("/cancelled"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        cache = app.state.response_cache
        assert not list(cache.redis.scan_iter(match=f"{cache.namespace}:public:*:lock"))
        cache.close()

    asyncio.run(scenario())


def test_real_read_timeout_is_bounded_and_does_not_fail_http(cache_settings):
    app = create_app(cache_settings)
    cache = app.state.response_cache
    cache.redis.ping()
    cache.redis.execute_command("CLIENT", "PAUSE", 350, "ALL")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = time.monotonic()
            task = asyncio.create_task(client.get("/info"))
            await asyncio.sleep(0.05)
            assert not task.done()
            response = await task
            assert response.status_code == 200
            assert response.headers["x-cache-bypass-reason"] == "cache_unavailable"
            assert time.monotonic() - started < 2
        await asyncio.sleep(0.4)
        cache.close()

    asyncio.run(scenario())
