"""Opt-in shared GET caching before dependency resolution."""

import json
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Literal

import anyio
from fastapi import Request, Response
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from tinypods_api.cache.constants import (
    CACHE_OUTCOME_HEADER,
    CACHE_POLICY_EXTENSION,
    CACHE_POLL_SECONDS,
    CACHE_REASON_HEADER,
    CACHE_WAIT_SECONDS,
    MAX_QUERY_BYTES,
    REQUEST_BYPASS_DIRECTIVES,
    STORABLE_RESPONSE_HEADERS,
    CacheOutcome,
)
from tinypods_api.http.constants import (
    CACHE_CONTROL_HEADER,
    CONTENT_TYPE_HEADER,
    JSON_MEDIA_TYPE,
    NO_STORE,
)
from tinypods_core.cache import CacheLookup, CacheUnavailable, ResponseCache
from tinypods_core.json_types import JsonRecord
from tinypods_core.settings import Settings


class CachePolicy(BaseModel):
    """Define the key identity revision and TTL bucket exported with a cached route.

    These immutable fields are interpreted by CachedReadRoute. They describe
    the public representation, not an authenticated principal or access grant.

    Attributes:
        namespace: Stable route-level grouping used in hashed response identities.
        revision: Representation revision; change it when stored content becomes incompatible.
        ttl: Configured content or metadata lifetime bucket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    namespace: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    revision: int = Field(ge=1)
    ttl: Literal["content", "metadata"]

    def openapi_extra(self) -> JsonRecord:
        """Build the OpenAPI extension that opts this endpoint into cache handling.

        Returns:
            Extension data containing the policy's namespace, revision and TTL bucket.
        """
        return {CACHE_POLICY_EXTENSION: self.model_dump()}

    def identity(self, request: Request) -> str:
        """Encode a request's route identity without ambiguous delimiter concatenation.

        Query names are sorted, but repeated values retain their relative order
        because parameter binding can depend on it. The storage layer hashes the
        result rather than putting raw paths/query values in Valkey keys.

        Args:
            request: GET request whose path parameters and query values identify the response.

        Returns:
            Canonical JSON text for this policy, path, parameters and ordered query values.
        """
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
        """Select the configured lifetime for this policy's TTL bucket.

        Args:
            settings: Validated content and metadata TTL configuration.

        Returns:
            The matching positive response lifetime in seconds.
        """
        return (
            settings.cache_metadata_ttl_seconds
            if self.ttl == "metadata"
            else settings.cache_ttl_seconds
        )


def tagged(response: Response, status: CacheOutcome, reason: str | None = None) -> Response:
    """Annotate a cache outcome and disable independent downstream storage.

    Args:
        response: Rendered or reconstructed HTTP response to annotate in place.
        status: HIT, MISS or BYPASS outcome shown in X-Cache.
        reason: Optional fixed bypass reason shown in X-Cache-Bypass-Reason.

    Returns:
        The same response with cache outcome headers and Cache-Control: no-store.
    """
    response.headers[CACHE_OUTCOME_HEADER] = status
    if reason:
        response.headers[CACHE_REASON_HEADER] = reason
    response.headers[CACHE_CONTROL_HEADER] = NO_STORE
    return response


def declared_queries(dependency: Dependant) -> set[str]:
    """Collect query aliases accepted by a route and its dependency tree.

    Args:
        dependency: FastAPI dependency definition for the selected endpoint.

    Returns:
        Accepted query names used to bypass unrecognized key-cardinality variations.
    """
    names = {field.alias for field in dependency.query_params}
    for child in dependency.dependencies:
        names.update(declared_queries(child))
    return names


class CachedReadRoute(APIRoute):
    """Wrap explicitly configured GET handlers with async response-cache access.

    Routes without the cache extension retain their original handler. For opted-in
    routes, a validated hit returns before dependency resolution; a miss invokes
    the original handler and stores only eligible public JSON responses. This
    wrapper bypasses credentials and declared cache-control requests but cannot
    prove a route is non-personalized: route authors own that contract.

    The application supplies settings and response_cache through app.state and
    closes the async pool in its lifespan. Fill-lock cleanup is awaited even
    when rendering fails or is cancelled.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        """Build an async cache wrapper only when an explicit policy is present.

        Returns:
            The original route handler for unannotated endpoints, otherwise an async
            Request-to-Response callable performing cache lookup and conditional fill.

        Raises:
            ValueError: A policy opts in a non-GET operation or lacks a response DTO.
            ValidationError: The explicit cache policy has invalid fields.
        """
        handler = super().get_route_handler()
        extra = self.openapi_extra or {}
        if CACHE_POLICY_EXTENSION not in extra:
            return handler
        policy = CachePolicy.model_validate(extra[CACHE_POLICY_EXTENSION])
        if self.methods != {"GET"}:
            raise ValueError("Only public GET routes may opt into caching")

        if not isinstance(self.response_model, type) or not issubclass(
            self.response_model, BaseModel
        ):
            raise ValueError("Cached routes require a public response DTO")
        adapter: TypeAdapter[BaseModel] = TypeAdapter(self.response_model)
        query_names = declared_queries(self.dependant)

        async def cached(request: Request) -> Response:
            """Read a valid cache hit or await the source handler and conditional publication.

            Credentials, explicit cache-control and undeclared query parameters bypass
            storage. A hit is validated/filtered through the response DTO before return.
            Only eligible 200 JSON bodies are published. Cache storage failures read
            through; endpoint failures still propagate to the application's handlers.

            Args:
                request: HTTP request with application-owned settings and cache state.

            Returns:
                A response with fresh outer request correlation and cache outcome headers.
            """
            settings: Settings = request.app.state.settings
            cache: ResponseCache | None = request.app.state.response_cache
            controls = {
                part.strip().lower()
                for part in request.headers.get(CACHE_CONTROL_HEADER, "").split(",")
            }
            reason = None
            if not settings.cache_enabled or cache is None:
                reason = "disabled"
            elif "authorization" in request.headers:
                reason = "authorization"
            elif "cookie" in request.headers:
                reason = "cookie"
            elif controls & REQUEST_BYPASS_DIRECTIVES:
                reason = "request_cache_control"
            elif len(request.scope.get("query_string", b"")) > MAX_QUERY_BYTES:
                reason = "query_too_long"
            # Ignored query strings must not allocate arbitrary entries on metadata routes.
            elif set(request.query_params) - query_names:
                reason = "undeclared_query"
            if reason:
                return tagged(await handler(request), CacheOutcome.BYPASS, reason)
            assert cache is not None
            key = policy.identity(request)
            deadline = time.monotonic() + CACHE_WAIT_SECONDS
            lookup: CacheLookup | None = None
            try:
                while True:
                    lookup = await cache.lookup(key)
                    if lookup.body is not None:
                        # Hits bypass FastAPI's source serializer.
                        # Validate and filter stored bytes before reconstructing a response.
                        try:
                            model = adapter.validate_json(lookup.body)
                        except ValidationError:
                            await cache.discard(lookup)
                            reason = "invalid_entry"
                            break
                        return tagged(
                            Response(adapter.dump_json(model), media_type=JSON_MEDIA_TYPE),
                            CacheOutcome.HIT,
                        )
                    if lookup.token or time.monotonic() >= deadline:
                        break
                    await anyio.sleep(CACHE_POLL_SECONDS)
            except CacheUnavailable:
                reason = "cache_unavailable"
            if reason:
                # Keep endpoint execution outside the storage exception boundary.
                # A source error must not trigger a second render or be misclassified as cache I/O.
                return tagged(await handler(request), CacheOutcome.BYPASS, reason)
            # Publication/release require a successfully observed lookup, not a failed read.
            assert lookup is not None
            try:
                response = await handler(request)
                body = getattr(response, "body", None)
                # Only body/standard content headers are reconstructed on a hit.
                # Cookies, Vary, private directives and custom headers cannot be discarded safely.
                if (
                    response.status_code == 200
                    and isinstance(body, bytes)
                    and response.headers.get(CONTENT_TYPE_HEADER, "").startswith(JSON_MEDIA_TYPE)
                    and not (set(response.headers) - STORABLE_RESPONSE_HEADERS)
                ):
                    with suppress(CacheUnavailable):
                        await cache.publish(lookup, body, policy.ttl_seconds(settings))
                return tagged(response, CacheOutcome.MISS)
            finally:
                # Await ownership cleanup on cancellation; socket deadlines still bound the attempt.
                with anyio.CancelScope(shield=True), suppress(CacheUnavailable):
                    await cache.release(lookup)

        return cached
