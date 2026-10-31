"""API composition root and process entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
import uvicorn
from fastapi import FastAPI, Response
from starlette.types import ASGIApp
from tinypods_api.auth import router as auth_router
from tinypods_api.cache import CachePolicy
from tinypods_api.http.constants import LIVENESS_PATH, READINESS_PATH
from tinypods_api.http.contracts import LivenessResponse, PlatformInfoResponse, ReadinessResponse
from tinypods_api.http.errors import ERROR_RESPONSES, install_error_handlers
from tinypods_api.http.middleware import RequestLoggingMiddleware
from tinypods_api.rate_limit import GlobalRateLimitMiddleware, RateLimitedRoute
from tinypods_core import __version__
from tinypods_core.auth import AuthService, Credentials, LocalPasswordProvider, SessionService
from tinypods_core.auth.federation import BrowserLogin
from tinypods_core.auth.providers.zitadel.oidc import ZitadelOIDC
from tinypods_core.cache import ResponseCache
from tinypods_core.db import Database
from tinypods_core.logging import configure_logging, shutdown_logging
from tinypods_core.rate_limit import RateLimiter
from tinypods_core.settings import ConfigurationError, Settings, load_settings

ReadinessCheck = Callable[[], Awaitable[bool]]
logger = logging.getLogger("tinypods.api")


class LoggedFastAPI(FastAPI):
    """Keep request context outside the error handler, including generated 500 responses."""

    def build_middleware_stack(self) -> ASGIApp:
        """Wrap the full ASGI stack so handled and unexpected errors retain correlation.

        Returns:
            Wrapped application with request logging outside error translation.
        """
        return RequestLoggingMiddleware(
            super().build_middleware_stack(), service="api", logger=logger
        )


def create_app(
    settings: Settings | None = None,
    *,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
) -> FastAPI:
    """Compose the API, optional cache pool, database and process lifecycle.

    Args:
        settings: Validated configuration, or None to load process environment settings.
        readiness_checks: Additional asynchronous dependency probes, keyed by name.

    Returns:
        A FastAPI application with typed probe/info routes and owned resource cleanup.

    Raises:
        ValueError: A supplied readiness name conflicts with a reserved dependency.
        ConfigurationError: Required process settings are invalid.
    """
    config = settings if settings is not None else load_settings()
    log_handler = configure_logging(config, service="api", non_blocking=True)
    checks = dict(readiness_checks or {})
    response_cache = (
        ResponseCache(config)
        if config.cache_enabled
        and (config.cache_valkey_url is not None or config.valkey_url is not None)
        else None
    )
    if "runtime" in checks:
        raise ValueError("runtime is a reserved readiness check name")

    database = Database(config) if config.database_url is not None else None
    limiter = (
        RateLimiter(config)
        if config.rate_limit_valkey_url is not None or config.valkey_url is not None
        else None
    )
    if database is not None:
        if "database" in checks:
            raise ValueError("database is a reserved readiness check name")

        async def database_ready() -> bool:
            """Probe the application-owned database through its async availability boundary.

            Returns:
                True when the configured migrated database responds successfully.
            """
            return await database.ping()

        checks["database"] = database_ready

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Mark runtime readiness and close every owned resource during shutdown.

        Args:
            app: Application whose readiness state changes with the process lifespan.

        Yields:
            None while the process is serving requests.

        Cleanup is shielded from cancellation. An async exit stack still closes the
        cache when database disposal fails, and drains logging after resource cleanup.
        """
        app.state.ready = True
        logger.info("API started")
        try:
            yield
        finally:
            app.state.ready = False
            # Run every cleanup even if another resource fails, then drain log output.
            with anyio.CancelScope(shield=True):
                async with AsyncExitStack() as cleanup:
                    cleanup.push_async_callback(asyncio.to_thread, log_handler.flush)
                    if response_cache is not None:
                        cleanup.push_async_callback(response_cache.close)
                    if database is not None:
                        cleanup.push_async_callback(database.close)
                    if limiter is not None:
                        cleanup.push_async_callback(limiter.close)
                    logger.info("API stopped")

    app = LoggedFastAPI(
        title="TinyPods API", version=__version__, lifespan=lifespan, responses=ERROR_RESPONSES
    )
    app.router.route_class = RateLimitedRoute
    app.add_middleware(
        GlobalRateLimitMiddleware, limiter=limiter, limit=config.rate_limit_global_per_minute
    )
    app.state.rate_limiter = limiter
    app.state.settings = config
    app.state.response_cache = response_cache
    install_error_handlers(app)
    app.state.database = database
    app.state.auth = (
        AuthService[Credentials](database, LocalPasswordProvider(database, limiter), limiter)
        if database is not None and config.auth_local_enabled
        else None
    )
    app.state.sessions = SessionService(database, settings=config) if database is not None else None
    app.state.browser_login = (
        BrowserLogin(database, config, ZitadelOIDC(config), limiter)
        if database is not None and config.zitadel_enabled
        else None
    )
    app.state.ready = False
    app.include_router(auth_router)

    @app.get(
        "/info",
        operation_id="getPlatformInfo",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(
            namespace="platform_info", revision=1, ttl="metadata"
        ).openapi_extra(),
    )
    async def platform_info() -> PlatformInfoResponse:
        """Return public service identity and the installed TinyPods version.

        The request has no path, query or body inputs. HTTP 200 returns service
        and version fields. X-Cache reports HIT, MISS or BYPASS. Cookie and
        Authorization requests bypass cache storage; Cache-Control: no-store
        prevents independent browser/proxy retention.

        Returns:
            Public service metadata from the installed core package.
        """
        return PlatformInfoResponse(version=__version__)

    @app.get(LIVENESS_PATH, operation_id="getLiveness", response_model=LivenessResponse)
    async def liveness() -> LivenessResponse:
        """Check that the API process can respond to HTTP requests.

        Returns HTTP 200 with status `alive`. This probe does not check startup
        readiness or dependencies; use `/readyz` to decide whether to send traffic.

        Returns:
            Process liveness without dependency checks.
        """
        return LivenessResponse()

    @app.get(
        READINESS_PATH,
        operation_id="getReadiness",
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
    )
    async def readiness(response: Response) -> ReadinessResponse:
        """Check whether the API runtime and configured dependencies are ready.

        Returns HTTP 200 with status `ready` when every check passes, otherwise
        HTTP 503 with status `not_ready`. Both responses include boolean results
        for each check, including `runtime` and PostgreSQL when configured.
        Failed or timed-out dependencies are reported as false without their
        exception details. Readiness failures do not change `/healthz`.

        Args:
            response: HTTP response whose status reflects aggregate readiness.

        Returns:
            Aggregate readiness plus individual dependency outcomes.
        """

        async def evaluate(name: str, check: ReadinessCheck) -> tuple[str, bool]:
            """Evaluate one readiness probe with a deadline and isolated dependency failures.

            Args:
                name: Stable dependency name included in readiness results.
                check: Async dependency probe invoked under the readiness deadline.

            Returns:
                Dependency name and whether its bounded probe succeeded.
            """
            try:
                async with asyncio.timeout(config.readiness_timeout):
                    return name, bool(await check())
            except Exception:
                logger.warning("Readiness dependency unavailable")
                return name, False

        results = dict(await asyncio.gather(*(evaluate(n, c) for n, c in checks.items())))
        results["runtime"] = bool(app.state.ready)
        ready = all(results.values())
        response.status_code = 200 if ready else 503
        return ReadinessResponse(status="ready" if ready else "not_ready", checks=results)

    return app


def main() -> None:
    """Load settings, run the HTTP server and tear down process logging."""
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    try:
        uvicorn.run(
            create_app(settings),
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
            access_log=False,
        )
    finally:
        shutdown_logging()
