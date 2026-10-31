"""Opt-in shared GET caching before dependency resolution."""

import json
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Literal

import anyio
from fastapi import Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from tinypods_core.cache import CacheUnavailable, ResponseCache
from tinypods_core.json_types import JsonRecord
from tinypods_core.settings import Settings

CACHE_POLICY_EXTENSION = "x-tinypods-cache"
WAIT_SECONDS = 1.0
POLL_SECONDS = 0.05


class CachePolicy(BaseModel):
    """Only public, non-personalized endpoints may explicitly opt in."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    namespace: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    revision: int = Field(ge=1)
    ttl: Literal["content", "metadata"]

    def openapi_extra(self) -> JsonRecord:
        return {CACHE_POLICY_EXTENSION: self.model_dump()}

    def identity(self, request: Request) -> str:
        return json.dumps(
            {
                "namespace": self.namespace,
                "revision": self.revision,
                "path": request.url.path,
                "parameters": sorted(
                    (name, str(value)) for name, value in request.path_params.items()
                ),
                # Preserve order among repeated values: it can affect parameter binding.
                "query": sorted(request.query_params.multi_items(), key=lambda pair: pair[0]),
            },
            separators=(",", ":"),
        )

    def ttl_seconds(self, settings: Settings) -> int:
        return (
            settings.cache_metadata_ttl_seconds
            if self.ttl == "metadata"
            else settings.cache_ttl_seconds
        )


def tagged(response: Response, status: str, reason: str | None = None) -> Response:
    response.headers["X-Cache"] = status
    if reason:
        response.headers["X-Cache-Bypass-Reason"] = reason
    response.headers["Cache-Control"] = "no-store"
    return response


class CachedReadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()
        extra = self.openapi_extra or {}
        if CACHE_POLICY_EXTENSION not in extra:
            return handler
        policy = CachePolicy.model_validate(extra[CACHE_POLICY_EXTENSION])
        if self.path in {"/healthz", "/readyz"} or self.methods != {"GET"}:
            raise ValueError("Only public GET routes other than probes may opt into caching")

        if not isinstance(self.response_model, type) or not issubclass(
            self.response_model, BaseModel
        ):
            raise ValueError("Cached routes require a public response DTO")
        adapter: TypeAdapter[BaseModel] = TypeAdapter(self.response_model)

        async def cached(request: Request) -> Response:
            settings: Settings = request.app.state.settings
            cache: ResponseCache | None = request.app.state.response_cache
            controls = {
                part.strip().lower() for part in request.headers.get("cache-control", "").split(",")
            }
            reason = None
            if not settings.cache_enabled or cache is None:
                reason = "disabled"
            elif "authorization" in request.headers:
                reason = "authorization"
            elif "cookie" in request.headers:
                reason = "cookie"
            elif controls & {"no-cache", "no-store"}:
                reason = "request_cache_control"
            elif len(request.scope.get("query_string", b"")) > 4096:
                reason = "query_too_long"
            if reason:
                return tagged(await handler(request), "BYPASS", reason)
            assert cache is not None
            key = policy.identity(request)
            deadline = time.monotonic() + WAIT_SECONDS
            try:
                while True:
                    lookup = await run_in_threadpool(cache.lookup, key)
                    if lookup.body is not None:
                        try:
                            model = adapter.validate_json(lookup.body)
                        except ValidationError:
                            await run_in_threadpool(cache.discard, lookup)
                            return tagged(await handler(request), "BYPASS", "invalid_entry")
                        return tagged(
                            Response(adapter.dump_json(model), media_type="application/json"), "HIT"
                        )
                    if lookup.token or time.monotonic() >= deadline:
                        break
                    await anyio.sleep(POLL_SECONDS)
            except CacheUnavailable:
                return tagged(await handler(request), "BYPASS", "cache_unavailable")
            try:
                response = await handler(request)
                body = getattr(response, "body", None)
                if (
                    response.status_code == 200
                    and isinstance(body, bytes)
                    and response.headers.get("content-type", "").startswith("application/json")
                    and not (set(response.headers) - {"content-type", "content-length"})
                ):
                    with suppress(CacheUnavailable):
                        await run_in_threadpool(
                            cache.publish, lookup, body, policy.ttl_seconds(settings)
                        )
                return tagged(response, "MISS")
            finally:
                with anyio.CancelScope(shield=True), suppress(CacheUnavailable):
                    await run_in_threadpool(cache.release, lookup)

        return cached
