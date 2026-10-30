"""Shared, bounded text/JSON logging."""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TextIO, TypedDict, cast
from uuid import UUID

from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.log_text import context_text, error_text, event_text, inline, local_time
from tinypods_core.settings import Settings

_context: ContextVar[dict[str, object] | None] = ContextVar("tinypods_log_context", default=None)
_EVENT = re.compile(r"[a-z][a-z0-9_]{0,100}\Z")
_APP_LOGGERS = ("tinypods.", "tinypods_core.", "tinypods_api.", "tinypods_worker.", "tinypods_cli.")
MAX_LINE = 16384

# Exclude logging machinery, not application-provided fields.
_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_RESERVED_FIELDS = {"timestamp", "level", "logger", "event", "pid", "exception"}


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """ContextVars also propagate from ASGI to FastAPI's synchronous thread pool."""
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def log_value(
    value: object,
    depth: int = 0,
    *,
    budget: list[int] | None = None,
) -> JsonValue:
    remaining = budget if budget is not None else [256]
    remaining[0] -= 1
    if remaining[0] < 0 or depth > 6:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, (list, tuple)):
        return [log_value(item, depth + 1, budget=remaining) for item in value[:30]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: log_value(item, depth + 1, budget=remaining)
            for key, item in list(value.items())[:40]
        }
    return f"<{type(value).__name__[:80]}>"


def log_message(record: logging.LogRecord) -> str:
    message = str(log_value(record.msg))
    if not record.args:
        return message
    arguments = log_value(record.args)
    return message % (tuple(arguments) if isinstance(arguments, list) else arguments)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "backend") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = self.payload(record)
        rendered = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        if len(rendered) <= MAX_LINE:
            return rendered
        # Retain correlation and fixed metadata without queuing an oversized payload.
        bounded = {
            key: value
            for key, value in payload.items()
            if key
            in {"timestamp", "level", "logger", "service", "event", "request_id", "command_id"}
        }
        bounded["truncated"] = True
        return json.dumps(bounded, ensure_ascii=True, allow_nan=False)

    def payload(self, record: logging.LogRecord) -> JsonRecord:
        fields = {**(_context.get() or {}), **record.__dict__}
        event = (
            record.msg
            if (isinstance(record.msg, str) and record.name.startswith(_APP_LOGGERS))
            else ""
        )
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "event": event if _EVENT.fullmatch(event) else "dependency_log",
            "pid": record.process,
            **{
                key: value
                for key, value in fields.items()
                if key not in _RECORD_FIELDS and key not in _RESERVED_FIELDS
            },
        }
        if payload["event"] == "dependency_log":
            payload["message"] = log_message(record)
            payload["location"] = f"{record.module}.{record.funcName}:{record.lineno}"
        if record.exc_info and record.exc_info[1] is not None:
            payload["error_type"] = type(record.exc_info[1]).__name__
            payload["exception"] = cast(JsonValue, exception_details(record.exc_info[1]))
        return cast(JsonRecord, log_value(payload))


class TextFormatter(JsonFormatter):
    def __init__(self, service: str, *, verbose: bool = False) -> None:
        super().__init__(service)
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        payload = self.payload(record)
        level = "WARN" if payload["level"] == "WARNING" else payload["level"]
        prefix = f"{local_time(payload['timestamp'])} {level:<5} [{inline(payload['service'])}]"
        message = event_text(payload)
        error = error_text(payload)
        if error:
            message += f" - {error}"
        details = context_text(payload)
        return (f"{prefix} {message}" + (f" ({'; '.join(details)})" if details else ""))[:MAX_LINE]

    def quiet(self, record: logging.LogRecord) -> bool:
        if self.verbose:
            return False
        if (
            self.service == "cli"
            and isinstance(record.msg, str)
            and record.msg in {"command_started", "command_completed"}
        ):
            return True
        return not record.name.startswith(_APP_LOGGERS) and record.levelno < logging.WARNING


class LogFrame(TypedDict):
    file: str
    line: int
    function: str


class LogException(TypedDict):
    type: str
    message: str
    frames: list[LogFrame]
    attribute: NotRequired[str]
    object_type: NotRequired[str]


def exception_details(exc: BaseException | None) -> list[LogException]:
    """Preserve exception messages, chained causes and bounded stack locations."""
    details: list[LogException] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(details) < 5:
        seen.add(id(exc))
        frames: list[LogFrame] = []
        for frame, lineno in traceback.walk_tb(exc.__traceback__):
            frames.append(
                {
                    "file": Path(frame.f_code.co_filename).name,
                    "line": lineno,
                    "function": frame.f_code.co_name,
                }
            )
        detail: LogException = {
            "type": type(exc).__name__,
            "message": str(exc),
            "frames": frames[-30:],
        }
        # Preserve interpreter-supplied identifiers without inspecting the object.
        if (
            isinstance(exc, AttributeError)
            and isinstance(exc.name, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exc.name)
        ):
            detail["attribute"] = exc.name
            owner = type(exc.obj).__name__
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", owner):
                detail["object_type"] = owner
        details.append(detail)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return details


if TYPE_CHECKING:
    _StderrHandlerBase = logging.StreamHandler[TextIO]
else:
    _StderrHandlerBase = logging.StreamHandler


class StderrHandler(_StderrHandlerBase):
    """Resolve stderr at emit time, including after CLI redirection or an RQ fork."""

    def emit(self, record: logging.LogRecord) -> None:
        if isinstance(self.formatter, TextFormatter) and self.formatter.quiet(record):
            return
        self.stream = sys.stderr
        super().emit(record)

    def flush(self) -> None:
        with suppress(OSError, ValueError):
            super().flush()

    def handleError(self, record: logging.LogRecord) -> None:
        # The stdlib fallback prints raw messages/arguments after formatting errors.
        # Never expose the failed record, including when the output stream is closed.
        with suppress(OSError, ValueError):
            sys.stderr.write("logging_error: unable to emit application log\n")


class QueuedStderrHandler(StderrHandler):
    """Bounded API log output: a stalled collector must not stall the event loop.

    Format on the caller to preserve ContextVars and serialize records before they
    enter the queue. Only bounded formatted strings reach the output thread.
    When full, discard new logs rather than block requests or grow memory forever.
    """

    def __init__(self, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("Logging queue capacity must be positive")
        super().__init__()
        self.pending: queue.Queue[tuple[TextIO, str] | threading.Event] = queue.Queue(
            maxsize=capacity
        )
        self.stopping = threading.Event()
        self.dropped = 0
        self.worker = threading.Thread(target=self._write_logs, name="api-log-output", daemon=True)
        self.worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        if self.stopping.is_set():
            return
        if isinstance(self.formatter, TextFormatter) and self.formatter.quiet(record):
            return
        try:
            line = self.format(record)
        except Exception:
            # Do not use StreamHandler's synchronous/raw-record error fallback.
            line = "logging_error: unable to format application log"
        try:
            self.pending.put_nowait((sys.stderr, line))
        except queue.Full:
            self.dropped += 1

    def _write_logs(self) -> None:
        while not self.stopping.is_set() or not self.pending.empty():
            try:
                item = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if isinstance(item, threading.Event):
                    item.set()
                else:
                    stream, line = item
                    with suppress(OSError, ValueError):
                        stream.write(line + "\n")
                        stream.flush()
            finally:
                self.pending.task_done()

    def flush(self) -> None:
        if self.stopping.is_set():
            return
        # Explicit drains (tests/process shutdown) are bounded even if stderr hangs.
        marker = threading.Event()
        try:
            self.pending.put(marker, timeout=1)
        except queue.Full:
            return
        marker.wait(timeout=1)

    def close(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        if threading.current_thread() is not self.worker:
            self.worker.join(timeout=1)
        super().close()


def configure_logging(
    settings: Settings,
    *,
    service: str = "backend",
    non_blocking: bool = False,
) -> StderrHandler:
    """Idempotent process setup, retaining unrelated embedding/test handlers."""
    level, log_format = settings.log_level, settings.log_format
    root = logging.getLogger()
    handler = next((item for item in root.handlers if isinstance(item, StderrHandler)), None)
    if handler is not None and (
        isinstance(handler, QueuedStderrHandler) != non_blocking
        or isinstance(handler, QueuedStderrHandler)
        and (handler.stopping.is_set() or handler.pending.maxsize != settings.log_queue_capacity)
    ):
        root.removeHandler(handler)
        handler.close()
        handler = None
    if handler is None:
        handler = (
            QueuedStderrHandler(settings.log_queue_capacity) if non_blocking else StderrHandler()
        )
        root.addHandler(handler)
    handler.setFormatter(
        JsonFormatter(service)
        if log_format == "json"
        else TextFormatter(
            service,
            verbose=level == "DEBUG",
        )
    )
    handler.setLevel(level)
    root.setLevel(level)
    logging.getLogger("tinypods").setLevel(level)
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate, access.disabled = False, True
    for name in ("uvicorn", "uvicorn.error", "alembic", "rq", "rq.worker", "rq.job"):
        dependency = logging.getLogger(name)
        dependency.handlers.clear()
        dependency.propagate, dependency.disabled = True, False
        dependency.setLevel(level)
    for name in ("httpcore", "httpcore2", "httpx", "httpx2", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def shutdown_logging() -> None:
    """Bounded process teardown, called outside the API event loop."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, StderrHandler):
            root.removeHandler(handler)
            handler.flush()
            handler.close()
