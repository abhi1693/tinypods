"""API composition root and process entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from starlette.types import ASGIApp
from tinypods_api.cache import CachedReadRoute, CachePolicy
from tinypods_api.contracts import LivenessResponse, PlatformInfoResponse, ReadinessResponse
from tinypods_api.errors import ERROR_RESPONSES, install_error_handlers
from tinypods_api.logging import RequestLoggingMiddleware
from tinypods_core import __version__
from tinypods_core.cache import ResponseCache
from tinypods_core.db import Database
from tinypods_core.logging import configure_logging, shutdown_logging
from tinypods_core.settings import ConfigurationError, Settings, load_settings

ReadinessCheck = Callable[[], Awaitable[bool]]
logger = logging.getLogger("tinypods.api")


class LoggedFastAPI(FastAPI):
    """Keep request context outside the error handler, including generated 500 responses."""

    def build_middleware_stack(self) -> ASGIApp:
        return RequestLoggingMiddleware(
            super().build_middleware_stack(), service="api", logger=logger
        )


def create_app(
    settings: Settings | None = None,
    *,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
) -> FastAPI:
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
    if database is not None:
        if "database" in checks:
            raise ValueError("database is a reserved readiness check name")

        async def database_ready() -> bool:
            return await database.ping()

        checks["database"] = database_ready

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = True
        logger.info("API started")
        try:
            yield
        finally:
            app.state.ready = False
            if database is not None:
                await database.close()
            if response_cache is not None:
                await asyncio.to_thread(response_cache.close)
            logger.info("API stopped")
            await asyncio.to_thread(log_handler.flush)

    app = LoggedFastAPI(
        title="TinyPods API", version=__version__, lifespan=lifespan, responses=ERROR_RESPONSES
    )
    app.router.route_class = CachedReadRoute
    app.state.settings = config
    app.state.response_cache = response_cache
    install_error_handlers(app)
    app.state.database = database
    app.state.ready = False

    @app.get(
        "/info",
        operation_id="getPlatformInfo",
        response_model=PlatformInfoResponse,
        openapi_extra=CachePolicy(
            namespace="platform_info", revision=1, ttl="metadata"
        ).openapi_extra(),
    )
    async def platform_info() -> PlatformInfoResponse:
        """Read public platform identity and the installed TinyPods version.

        Returns HTTP 200 with non-personalized service metadata. Eligible GET
        requests may use the optional server-side cache; browsers and proxies
        receive Cache-Control: no-store. Cookie and authorization requests bypass it.
        """
        return PlatformInfoResponse(version=__version__)

    @app.get("/healthz", operation_id="getLiveness", response_model=LivenessResponse)
    async def liveness() -> LivenessResponse:
        """Check that the API process can respond to HTTP requests.

        Returns HTTP 200 with status `alive`. This probe does not check startup
        readiness or dependencies; use `/readyz` to decide whether to send traffic.
        """
        return LivenessResponse()

    @app.get(
        "/readyz",
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
        """

        async def evaluate(name: str, check: ReadinessCheck) -> tuple[str, bool]:
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
