"""Optional installation-wide HTTP request budget using the shared limiter."""

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send
from tinypods_api.http.errors import error_response
from tinypods_core.rate_limit import RateLimiter, RateLimitUnavailable, RatePolicy


class GlobalRateLimitMiddleware:
    """Apply a configurable aggregate budget to HTTP requests, excluding health probes."""

    def __init__(self, app: ASGIApp, *, limiter: RateLimiter | None, limit: int) -> None:
        """Retain the application-owned limiter and optional requests-per-minute budget."""
        self.app = app
        self.limiter = limiter
        self.policy = RatePolicy("http:global", limit, 60_000) if limit else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reserve before routing; render safe correlated errors outside route handlers."""
        if (
            scope["type"] != "http"
            or self.policy is None
            or scope["path"] in {"/healthz", "/readyz"}
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.limiter is None:
            response = error_response(request, 503, "http_503", "Service Unavailable")
        else:
            try:
                decision = await self.limiter.reserve(self.policy, "installation")
            except RateLimitUnavailable:
                response = error_response(request, 503, "http_503", "Service Unavailable")
            else:
                if decision.allowed:
                    await self.app(scope, receive, send)
                    return
                response = error_response(
                    request,
                    429,
                    "http_429",
                    "Too Many Requests",
                    headers={"Retry-After": str(decision.retry_after)},
                )
        await response(scope, receive, send)
