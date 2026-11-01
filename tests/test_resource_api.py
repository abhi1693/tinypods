"""Versioned resource routing and common HTTP/security policy contracts."""

from types import SimpleNamespace
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from fastapi import Query
from fastapi.testclient import TestClient
from pydantic import BaseModel
from tinypods_api.cache import CachePolicy
from tinypods_api.http import (
    API_V1_PREFIX,
    Page,
    PageParams,
    RequestDTO,
    ResourceAction,
    ResourceRouter,
)
from tinypods_api.http.schema import export_schema
from tinypods_api.main import create_app
from tinypods_api.rate_limit import EndpointRatePolicy
from tinypods_core.auth import AuthError, Principal
from tinypods_core.settings import Settings


class ItemWrite(RequestDTO):
    """Explicit public payload for a test-only resource."""

    name: str


class ItemRead(BaseModel):
    """Explicit representation for a test-only resource."""

    id: UUID
    name: str


class FakeSessions:
    """Exercise the real principal dependency without a database in router unit tests."""

    async def authenticate(self, token: str, *, csrf_token: str | None = None) -> Principal:
        """Check the test session and CSRF credentials."""
        if token != "session":
            raise AuthError(401)
        if csrf_token is not None and csrf_token != "csrf":
            raise AuthError(403)
        return Principal(uuid4(), "test", True, "test-session")


class FakeLimiter:
    """Record policy reservations without timing-dependent external I/O."""

    def __init__(self) -> None:
        """Keep an ordered observation of the handler pipeline."""
        self.allowed = True
        self.policies = []

    async def reserve(self, policy: object, peer: str) -> SimpleNamespace:
        """Record the budget and return a controlled decision."""
        self.policies.append(policy)
        return SimpleNamespace(allowed=self.allowed, retry_after=2)

    async def close(self) -> None:
        """Own no external resources."""


def test_resource_actions_keep_typed_contracts_and_security() -> None:
    """All six actions share responses, authentication, CSRF, errors and rate limits."""
    app = create_app(Settings(environment="development"))
    limiter = FakeLimiter()
    app.state.rate_limiter = limiter
    app.state.sessions = FakeSessions()
    router = ResourceRouter(domain="test", resource="item", prefix="/items")
    item = ItemRead(id=uuid4(), name="item")

    @router.action(ResourceAction.LIST, response_model=Page[ItemRead])
    async def list_items(page: Annotated[PageParams, Query()]) -> Page[ItemRead]:
        """Return a bounded deterministic list of test items."""
        return Page(items=[item], total=1, limit=page.limit, offset=page.offset)

    @router.action(ResourceAction.RETRIEVE, response_model=ItemRead)
    async def retrieve_item(id: UUID) -> ItemRead:
        """Retrieve the test item by its public identifier."""
        return item

    @router.action(ResourceAction.CREATE, response_model=ItemRead)
    async def create_item(payload: ItemWrite) -> ItemRead:
        """Create a test representation from a validated payload."""
        return ItemRead(id=item.id, name=payload.name)

    @router.action(ResourceAction.REPLACE, response_model=ItemRead)
    async def replace_item(id: UUID, payload: ItemWrite) -> ItemRead:
        """Replace the test representation from a complete payload."""
        return ItemRead(id=id, name=payload.name)

    @router.action(ResourceAction.UPDATE, response_model=ItemRead)
    async def update_item(id: UUID, payload: ItemWrite) -> ItemRead:
        """Apply a supplied test value through the same public DTO."""
        return ItemRead(id=id, name=payload.name)

    @router.action(ResourceAction.DELETE)
    async def delete_item(id: UUID) -> None:
        """Remove the test item with an empty success response."""

    app.include_router(router, prefix=API_V1_PREFIX)
    schema = export_schema(app)
    paths = schema["paths"]
    assert set(paths["/api/v1/items"]) >= {"get", "post"}
    assert set(paths["/api/v1/items/{id}"]) >= {"get", "put", "patch", "delete"}
    assert paths["/api/v1/items"]["post"]["operationId"] == "testItemCreate"
    assert "ErrorResponse" in str(paths["/api/v1/items"]["post"]["responses"])
    assert paths["/api/v1/items"]["get"]["x-tinypods-rate-limit"]["limit"] == 120
    assert paths["/api/v1/items"]["post"]["x-tinypods-rate-limit"]["limit"] == 30
    assert paths["/api/v1/items"]["post"]["parameters"][0]["name"] == "x-csrf-token"
    with TestClient(app) as client:
        assert client.get("/api/v1/items").status_code == 401
        assert len(limiter.policies) == 1
        client.cookies.set("tinypods-session", "session")
        assert client.get("/api/v1/items?limit=1&offset=0").json()["limit"] == 1
        assert client.get("/api/v1/items?limit=101").status_code == 422
        assert client.get("/api/v1/items/" + str(item.id)).status_code == 200
        assert client.post("/api/v1/items", json={"name": "new"}).status_code == 403
        headers = {"x-csrf-token": "csrf"}
        assert (
            client.post("/api/v1/items", json={"name": "new"}, headers=headers).status_code == 201
        )
        assert (
            client.post(
                "/api/v1/items", json={"name": "new", "private": "secret"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/items",
                json={"name": "new"},
                headers={**headers, "origin": "https://foreign.test"},
            ).status_code
            == 403
        )
        for method in ("put", "patch"):
            assert (
                getattr(client, method)(
                    "/api/v1/items/" + str(item.id), json={"name": "updated"}, headers=headers
                ).json()["name"]
                == "updated"
            )
        deleted = client.delete("/api/v1/items/" + str(item.id), headers=headers)
        assert deleted.status_code == 204 and deleted.content == b""
        limiter.allowed = False
        denied = client.get("/api/v1/items")
        assert denied.status_code == 429 and denied.headers["retry-after"] == "2"
        app.state.rate_limiter = None
        assert client.get("/api/v1/items").status_code == 503


def test_public_cache_and_rate_policy_compose_before_handler() -> None:
    """Rate reservations still precede public cache hits and writes stay protected."""
    app = create_app(Settings(environment="development"))
    app.state.rate_limiter = FakeLimiter()
    app.state.sessions = FakeSessions()
    router = ResourceRouter(
        domain="test", resource="publicItem", prefix="/public", public_reads=True
    )
    called = []
    item = ItemRead(id=uuid4(), name="cached")

    class Cache:
        """Return a single valid cache hit without external I/O."""

        async def lookup(self, key: str) -> SimpleNamespace:
            """Record lookup ordering."""
            assert len(app.state.rate_limiter.policies) > 0
            called.append("lookup")
            return SimpleNamespace(body=item.model_dump_json().encode())

        async def close(self) -> None:
            """Own no external resources."""

    app.state.response_cache = Cache()

    @router.action(
        ResourceAction.LIST,
        response_model=ItemRead,
        cache=CachePolicy(namespace="test_public", revision=1, ttl="metadata"),
        rate=EndpointRatePolicy(name="public-read", limit=5),
    )
    async def public_item() -> ItemRead:
        """Return the identity-independent public test representation."""
        called.append("handler")
        return item

    @router.action(ResourceAction.CREATE, response_model=ItemRead)
    async def create_public_item(payload: ItemWrite) -> ItemRead:
        """Require authentication even when collection reads are public."""
        return item

    app.include_router(router, prefix=API_V1_PREFIX)
    with TestClient(app) as client:
        hit = client.get("/api/v1/public")
        assert hit.status_code == 200 and hit.headers["x-cache"] == "HIT"
        assert called == ["lookup"]
        assert app.state.rate_limiter.policies[-1].limit == 5
        # Credentials bypass shared caching even for explicitly public reads.
        assert (
            client.get("/api/v1/public", headers={"authorization": "test"}).headers["x-cache"]
            == "BYPASS"
        )
        assert called == ["lookup", "handler"]
        assert client.post("/api/v1/public", json={"name": "new"}).status_code == 401


def test_invalid_resource_contracts_are_rejected() -> None:
    """Fail startup for unsafe caching, missing DTOs or duplicate action contracts."""
    router = ResourceRouter(domain="test", resource="item", prefix="/items")
    for action, response in ((ResourceAction.LIST, None), (ResourceAction.DELETE, ItemRead)):
        with pytest.raises(ValueError, match="DTO"):
            router.action(action, response_model=response)
    policy = CachePolicy(namespace="unsafe", revision=1, ttl="content")
    with pytest.raises(ValueError, match="public reads"):
        router.action(ResourceAction.LIST, response_model=ItemRead, cache=policy)
    public = ResourceRouter(domain="test", resource="item", prefix="/items", public_reads=True)
    with pytest.raises(ValueError, match="public reads"):
        public.action(ResourceAction.CREATE, response_model=ItemRead, cache=policy)

    @router.action(ResourceAction.LIST, response_model=ItemRead)
    async def item() -> ItemRead:
        """Return a typed test item."""
        return ItemRead(id=uuid4(), name="test")

    with pytest.raises(ValueError, match="already registered"):
        router.action(ResourceAction.LIST, response_model=ItemRead)
    with pytest.raises(ValueError, match="lowerCamelCase"):
        ResourceRouter(domain="invalid-domain", resource="item", prefix="/items")


def test_only_business_routes_are_versioned() -> None:
    """Keep probe paths stable and reject removed unversioned business URLs."""
    app = create_app(Settings())
    schema = export_schema(app)
    assert "/api/v1/info" in schema["paths"]
    assert "/api/v1/auth/providers" in schema["paths"]
    assert "/healthz" in schema["paths"] and "/readyz" in schema["paths"]
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/info").status_code == 200
        assert (
            client.get("/api/v1/auth/providers")
            .json()["data"][0]["login_path"]
            .startswith("/api/v1/")
        )
        assert client.get("/info").status_code == 404
        assert client.get("/auth/providers").status_code == 404
