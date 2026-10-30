"""ASGI request logging keeps context alive through response bodies and failures."""

from __future__ import annotations

import logging
import time
import uuid
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tinypods_core.logging import elapsed_ms, log_context


class RequestLoggingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str,
        logger: logging.Logger,
        response_headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        self.app = app
        self.service = service
        self.logger = logger
        self.response_headers = response_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())  # Do not trust client-supplied correlation IDs.
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500
        failed = False

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                replaced = {key.lower() for key, _ in self.response_headers} | {b"x-request-id"}
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() not in replaced
                ]
                headers.extend(self.response_headers)
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        fields = request_log_fields(scope)
        with log_context(service=self.service, request_id=request_id, **fields):
            try:
                await self.app(scope, receive, send_response)
            except Exception:
                failed = True
                self.logger.exception("request_failed")
                raise
            finally:
                level = (
                    logging.ERROR
                    if failed or status >= 500
                    else (logging.WARNING if status >= 400 else logging.INFO)
                )
                if status < 400 and not failed and scope.get("path") in {"/healthz", "/readyz"}:
                    level = logging.DEBUG
                self.logger.log(
                    level,
                    "request_completed",
                    extra={
                        "status_code": status,
                        "duration_ms": elapsed_ms(started),
                    },
                )


def request_log_fields(scope: Scope) -> dict[str, str]:
    """Record method and path without collecting request headers, bodies or query values."""
    raw_path = scope.get("raw_path")
    path = (
        quote(raw_path.decode("ascii", "replace"), safe="/%:@!$&'()*+,;=-._~")
        if isinstance(raw_path, bytes)
        else quote(scope.get("path", "/"), safe="/:@!$&'()*+,;=-._~")
    )
    return {"method": scope["method"], "route": path}
