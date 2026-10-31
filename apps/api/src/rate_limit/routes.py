"""Opt-in endpoint rate policies applied before dependencies and response caching."""

from collections.abc import Callable, Coroutine

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from tinypods_api.cache import CachedReadRoute
from tinypods_core.json_types import JsonRecord
from tinypods_core.rate_limit import RateLimiter, RatePolicy

POLICY_EXTENSION = "x-tinypods-rate-limit"


class EndpointRatePolicy(BaseModel):
    """Declare an immutable per-client endpoint budget, analogous to CachePolicy."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-zA-Z0-9:_-]{1,64}$")
    limit: int = Field(ge=1, le=1_000_000)
    window_ms: int = Field(default=60_000, ge=1, le=86_400_000)

    def openapi_extra(self) -> JsonRecord:
        """Opt a route into a named endpoint budget without a wrapper signature."""
        return {POLICY_EXTENSION: self.model_dump()}


class RateLimitedRoute(CachedReadRoute):
    """Enforce endpoint limits before even cached hits; preserve ordinary route behavior."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        """Compose an optional endpoint reservation around the cache/HTTP handler."""
        handler = super().get_route_handler()
        extra = self.openapi_extra or {}
        if POLICY_EXTENSION not in extra:
            return handler
        policy = EndpointRatePolicy.model_validate(extra[POLICY_EXTENSION])
        budget = RatePolicy("endpoint:" + policy.name, policy.limit, policy.window_ms)

        async def limited(request: Request) -> Response:
            """Reserve per transport peer; never trust raw forwarded headers."""
            limiter: RateLimiter | None = request.app.state.rate_limiter
            if limiter is None:
                raise HTTPException(503)
            peer = request.client.host if request.client else "unknown"
            decision = await limiter.reserve(budget, peer)
            if not decision.allowed:
                raise HTTPException(429, headers={"Retry-After": str(decision.retry_after)})
            return await handler(request)

        return limited
