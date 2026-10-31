"""ASGI request logging keeps context alive through response bodies and failures."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tinypods_api.http.constants import QUIET_PROBE_PATHS, REQUEST_ID_HEADER_BYTES
from tinypods_core.logging import elapsed_ms, failure_fields, log_context
from tinypods_core.logging.constants import LogEvent, LogField


class RequestLoggingMiddleware:
    """Keep correlation active through the complete response, errors and cancellation."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str,
        logger: logging.Logger,
        response_headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        """Retain the downstream application, request logger and response-header policy.

        Args:
            app: Application or downstream ASGI callable being composed.
            service: Process service label included in output records.
            logger: Logger receiving request lifecycle events.
            response_headers: Header pairs replaced on outgoing HTTP responses.
        """
        self.app = app
        self.service = service
        self.logger = logger
        self.response_headers = response_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the downstream ASGI application with correlated lifecycle logging.

        Args:
            scope: ASGI scope containing request method, path and state.
            receive: ASGI callable receiving incoming protocol messages.
            send: ASGI callable forwarding outgoing protocol messages.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())  # Do not trust client-supplied correlation IDs.
        scope.setdefault("state", {})[LogField.REQUEST_ID] = request_id
        started = time.perf_counter()
        status = 500
        failed = False
        cancelled = False

        async def send_response(message: Message) -> None:
            """Replace correlation headers and remember the status sent to the client.

            Args:
                message: Outgoing ASGI response-start or response-body message.
            """
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                replaced = {key.lower() for key, _ in self.response_headers} | {
                    REQUEST_ID_HEADER_BYTES
                }
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() not in replaced
                ]
                headers.extend(self.response_headers)
                headers.append((REQUEST_ID_HEADER_BYTES, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        fields = request_log_fields(scope)
        with log_context(service=self.service, request_id=request_id, **fields):
            try:
                await self.app(scope, receive, send_response)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                failed = True
                self.logger.error(LogEvent.REQUEST_FAILED, extra=failure_fields(exc))
                raise
            finally:
                level = (
                    logging.ERROR
                    if failed or status >= 500
                    else (logging.WARNING if status >= 400 else logging.INFO)
                )
                if cancelled:
                    level = logging.INFO
                if (
                    status < 400
                    and not failed
                    and not cancelled
                    and scope.get("path") in QUIET_PROBE_PATHS
                ):
                    level = logging.DEBUG
                self.logger.log(
                    level,
                    LogEvent.REQUEST_CANCELLED if cancelled else LogEvent.REQUEST_COMPLETED,
                    extra={
                        LogField.STATUS: status,
                        LogField.DURATION: elapsed_ms(started),
                    },
                )


def request_log_fields(scope: Scope) -> dict[str, str]:
    """Record method and path without collecting request headers, bodies or query values.

    Args:
        scope: ASGI scope containing request method, path and state.

    Returns:
        Selected method and escaped request path fields.
    """
    raw_path = scope.get("raw_path")
    path = (
        quote(raw_path.decode("ascii", "replace"), safe="/%:@!$&'()*+,;=-._~")
        if isinstance(raw_path, bytes)
        else quote(scope.get("path", "/"), safe="/:@!$&'()*+,;=-._~")
    )
    return {LogField.METHOD: scope[LogField.METHOD], LogField.ROUTE: path}
