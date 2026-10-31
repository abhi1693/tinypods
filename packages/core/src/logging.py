"""Shared full-content text/JSON logging with optional queued output."""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NotRequired, TextIO, TypedDict, cast

from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.log_constants import (
    QUIET_COMMAND_EVENTS,
    TEXT_CONTEXT_FIELDS,
    LogEvent,
    LogField,
)
from tinypods_core.log_text import context_text, error_text, event_text, inline, local_time
from tinypods_core.log_values import encode_json, message_argument
from tinypods_core.log_values import log_value as log_value
from tinypods_core.settings import Settings

_context: ContextVar[dict[str, object] | None] = ContextVar("tinypods_log_context", default=None)
_EVENT = re.compile(r"[a-z][a-z0-9_]{0,100}\Z")
_APP_LOGGERS = ("tinypods.", "tinypods_core.", "tinypods_api.", "tinypods_worker.", "tinypods_cli.")

# Exclude logging machinery, not application-provided fields.
_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {LogField.MESSAGE, "asctime"}
_RESERVED_FIELDS = {"timestamp", LogField.LEVEL, "logger", LogField.EVENT, "pid", "exception"}


def elapsed_ms(started: float) -> float:
    """Measure elapsed monotonic time in rounded milliseconds.

    Args:
        started: Monotonic start time returned by time.perf_counter.

    Returns:
        Elapsed milliseconds, rounded to two decimal places.
    """
    return round((time.perf_counter() - started) * 1000, 2)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Merge task-local log fields and restore the previous context on every exit.

    ContextVars also propagate into FastAPI synchronous thread-pool work.

    Args:
        fields: Context fields merged into the current task-local logging scope.

    Yields:
        None while the merged task-local context remains active.
    """
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def log_message(record: logging.LogRecord) -> str:
    """Interpolate complete logging arguments without inspecting arbitrary object internals.

    Args:
        record: Logging record to format or emit.

    Returns:
        A complete formatted line without size-based shortening.
    """
    normalized = log_value(record.msg)
    message = normalized if isinstance(normalized, str) else encode_json(normalized)
    if not record.args:
        return message
    arguments = log_value(record.args)
    if isinstance(arguments, list):
        return message % tuple(message_argument(value) for value in arguments)
    if isinstance(arguments, dict):
        return message % {key: message_argument(value) for key, value in arguments.items()}
    return message % message_argument(arguments)


class BaseFormatter(logging.Formatter, ABC):
    """Normalize process context and exception information for both output formats."""

    def __init__(self, service: str = "backend") -> None:
        """Retain the service label used when assembling structured records.

        Args:
            service: Process service label included in output records.
        """
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        """Render a complete record in the subclass output format.

        Args:
            record: Logging record containing the message and diagnostic fields.

        Returns:
            Complete escaped output without application-imposed length limits.
        """
        return self.render(self.payload(record))

    @abstractmethod
    def render(self, payload: JsonRecord) -> str:
        """Render an owned normalized snapshot in the selected output format.

        Args:
            payload: Complete JSON-compatible snapshot captured in the emitting context.

        Returns:
            Complete escaped output without application-imposed length limits.
        """
        raise NotImplementedError

    def payload(self, record: logging.LogRecord) -> JsonRecord:
        """Combine record metadata and task context into complete JSON-compatible fields.

        Args:
            record: Logging record to format or emit.

        Returns:
            Complete metadata, context, event and optional exception fields.
        """
        fields = {**(_context.get() or {}), **record.__dict__}
        event = (
            record.msg
            if (isinstance(record.msg, str) and record.name.startswith(_APP_LOGGERS))
            else ""
        )
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            LogField.LEVEL: record.levelname,
            "logger": record.name,
            LogField.SERVICE: self.service,
            LogField.EVENT: event if _EVENT.fullmatch(event) else LogEvent.DEPENDENCY,
            "pid": record.process,
            **{
                key: value
                for key, value in fields.items()
                if key not in _RECORD_FIELDS and key not in _RESERVED_FIELDS
            },
        }
        if payload[LogField.EVENT] == LogEvent.DEPENDENCY:
            payload[LogField.MESSAGE] = log_message(record)
            payload["location"] = f"{record.module}.{record.funcName}:{record.lineno}"
        if record.exc_info and record.exc_info[1] is not None:
            payload["error_type"] = type(record.exc_info[1]).__name__
            payload["exception"] = cast(JsonValue, exception_details(record.exc_info[1]))
        return cast(JsonRecord, log_value(payload))


class JsonFormatter(BaseFormatter):
    """Serialize the shared complete payload as escaped single-line JSON."""

    def render(self, payload: JsonRecord) -> str:
        """Render all normalized fields and exception information.

        Args:
            payload: Complete normalized record captured by the shared base formatter.

        Returns:
            Complete JSON without field, collection or line-length truncation.
        """
        return encode_json(payload)


class TextFormatter(BaseFormatter):
    """Render operational events as compact human-readable log lines."""

    def __init__(self, service: str, *, verbose: bool = False) -> None:
        """Retain the service label and dependency-event verbosity policy.

        Args:
            service: Process service label included in output records.
            verbose: Whether low-value dependency and CLI events remain visible.
        """
        super().__init__(service)
        self.verbose = verbose

    def render(self, payload: JsonRecord) -> str:
        """Render the complete normalized record in readable text.

        Args:
            payload: Complete normalized record captured by the shared base formatter.

        Returns:
            A complete formatted line without size-based shortening.
        """
        level = "WARN" if payload[LogField.LEVEL] == "WARNING" else payload[LogField.LEVEL]
        prefix = (
            f"{local_time(payload['timestamp'])} {level:<5} [{inline(payload[LogField.SERVICE])}]"
        )
        message = event_text(payload)
        error = error_text(payload)
        if error:
            message += f" - {error}"
        details = context_text(payload)
        details.extend(
            f"{inline(key)}={inline(value)}"
            for key, value in payload.items()
            if key not in TEXT_CONTEXT_FIELDS
        )
        return f"{prefix} {message}" + (f" ({'; '.join(details)})" if details else "")

    def quiet(self, record: logging.LogRecord) -> bool:
        """Identify low-value dependency and CLI events suppressed from normal text output.

        Args:
            record: Logging record whose level and origin determine normal text visibility.

        Returns:
            True when normal text output should suppress this record.
        """
        if self.verbose:
            return False
        if (
            self.service == "cli"
            and isinstance(record.msg, str)
            and record.msg in QUIET_COMMAND_EVENTS
        ):
            return True
        return not record.name.startswith(_APP_LOGGERS) and record.levelno < logging.WARNING


class LogFrame(TypedDict):
    """Describe one traceback location without retaining live frame objects."""

    file: str
    line: int
    function: str


class LogException(TypedDict):
    """Describe complete exception diagnostics at the JSON serialization boundary."""

    type: str
    message: str
    frames: list[LogFrame]
    exceptions: NotRequired[list[list[LogException]]]
    cycle: NotRequired[bool]
    arguments: NotRequired[JsonValue]
    attribute: NotRequired[str]
    object_type: NotRequired[str]


def exception_details(exc: BaseException | None) -> list[LogException]:
    """Preserve complete exception messages, chained causes and stack locations.

    Args:
        exc: Exception being translated or summarized at this boundary.

    Returns:
        Complete exception chain, ordered from outer error to cause.
    """
    details: list[LogException] = []
    pending: list[tuple[BaseException, list[LogException], frozenset[int]]] = (
        [(exc, details, frozenset())] if exc is not None else []
    )
    while pending:
        current, destination, ancestors = pending.pop()
        if id(current) in ancestors:
            destination.append(
                {
                    "type": type(current).__name__,
                    "message": "<exception chain cycle>",
                    "frames": [],
                    "cycle": True,
                }
            )
            continue
        ancestors = ancestors | {id(current)}
        frames: list[LogFrame] = [
            {
                "file": frame.f_code.co_filename,
                "line": lineno,
                "function": frame.f_code.co_name,
            }
            for frame, lineno in traceback.walk_tb(current.__traceback__)
        ]
        try:
            message = str(current)
        except Exception as conversion_error:
            # A custom exception string hook may fail; retain its original arguments.
            message = f"<exception message conversion failed: {type(conversion_error).__name__}>"
        detail: LogException = {
            "type": type(current).__name__,
            "message": message,
            "frames": frames,
            "arguments": log_value(current.args),
        }
        if isinstance(current, AttributeError) and isinstance(current.name, str):
            detail["attribute"] = current.name
            detail["object_type"] = type(current.obj).__name__
        destination.append(detail)
        cause = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if cause is not None:
            pending.append((cause, destination, ancestors))
        if isinstance(current, BaseExceptionGroup):
            groups: list[list[LogException]] = [[] for _ in current.exceptions]
            detail["exceptions"] = groups
            pending.extend(
                (child, target, ancestors)
                for child, target in reversed(tuple(zip(current.exceptions, groups, strict=True)))
            )
    return details


def failure_fields(exc: BaseException) -> JsonRecord:
    """Build source-safe failure diagnostics without forwarding arbitrary exception text.

    Request and CLI boundaries can receive credentials or user input inside third-party
    exception messages. They log the error type and complete locations deliberately,
    without submitting that raw exception or its arguments to any formatter.

    Args:
        exc: Boundary failure whose message is not a safe logging contract.

    Returns:
        Exception type and full traceback locations, without messages, arguments or locals.
    """
    return {
        "error_type": type(exc).__name__,
        "traceback": [
            {
                "file": frame.f_code.co_filename,
                "line": lineno,
                "function": frame.f_code.co_name,
            }
            for frame, lineno in traceback.walk_tb(exc.__traceback__)
        ],
    }


if TYPE_CHECKING:
    _StderrHandlerBase = logging.StreamHandler[TextIO]
else:
    _StderrHandlerBase = logging.StreamHandler


@dataclass(frozen=True)
class PreparedLog:
    """Own a normalized snapshot and its renderer until asynchronous output completes.

    Attributes:
        stream: Output destination captured when the record was emitted.
        formatter: Renderer selected at emission, independent of later reconfiguration.
        payload: Private normalized data that callers can no longer mutate.
    """

    stream: TextIO
    formatter: BaseFormatter
    payload: JsonRecord


class StderrHandler(_StderrHandlerBase):
    """Resolve stderr at emit time, including after CLI redirection or an RQ fork."""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit the record through the handler-owned output path.

        Args:
            record: Logging record to format or emit.
        """
        if isinstance(self.formatter, TextFormatter) and self.formatter.quiet(record):
            return
        self.stream = sys.stderr
        super().emit(record)

    def flush(self) -> None:
        """Drain pending output within the handler shutdown bounds."""
        with suppress(OSError, ValueError):
            super().flush()

    def handleError(self, record: logging.LogRecord) -> None:
        """Emit a fixed failure notice without replaying an unformattable record.

        Args:
            record: Logging record to format or emit.
        """
        # The stdlib fallback prints raw messages/arguments after formatting errors.
        # Never expose the failed record, including when the output stream is closed.
        with suppress(OSError, ValueError):
            sys.stderr.write("logging_error: unable to emit application log\n")


class QueuedStderrHandler(StderrHandler):
    """Queued API log output: a stalled collector must not stall the event loop.

    Normalize on the caller to snapshot ContextVars and mutable values. Render and
    escape that owned snapshot on the output thread.
    When full, discard new logs rather than block requests or grow memory forever.
    """

    def __init__(self, capacity: int = 1024) -> None:
        """Start a daemon output thread backed by a bounded queue.

        Args:
            capacity: Maximum number of queued log lines or flush markers.
        """
        if capacity < 1:
            raise ValueError("Logging queue capacity must be positive")
        super().__init__()
        self.pending: queue.Queue[PreparedLog | tuple[TextIO, str] | threading.Event] = queue.Queue(
            maxsize=capacity
        )
        self.stopping = threading.Event()
        self.dropped = 0
        self.worker = threading.Thread(target=self._write_logs, name="api-log-output", daemon=True)
        self.worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Emit the record through the handler-owned output path.

        Args:
            record: Logging record to format or emit.
        """
        if self.stopping.is_set():
            return
        if isinstance(self.formatter, TextFormatter) and self.formatter.quiet(record):
            return
        output: PreparedLog | tuple[TextIO, str]
        try:
            if isinstance(self.formatter, BaseFormatter):
                # Capture task context and mutable fields now; expensive escaping/rendering
                # runs on the output thread, against this private normalized snapshot.
                output = PreparedLog(sys.stderr, self.formatter, self.formatter.payload(record))
            else:
                output = (sys.stderr, self.format(record))
        except Exception:
            # Do not replay raw records if a custom formatter or conversion fails.
            output = (sys.stderr, "logging_error: unable to prepare application log")
        try:
            self.pending.put_nowait(output)
        except queue.Full:
            self.dropped += 1

    def _write_logs(self) -> None:
        """Drain formatted strings off the event loop and acknowledge flush barriers."""
        while not self.stopping.is_set() or not self.pending.empty():
            try:
                item = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if isinstance(item, threading.Event):
                    item.set()
                else:
                    if isinstance(item, PreparedLog):
                        stream = item.stream
                        try:
                            line = item.formatter.render(item.payload)
                        except Exception:
                            line = "logging_error: unable to render application log"
                    else:
                        stream, line = item
                    with suppress(OSError, ValueError):
                        stream.write(line + "\n")
                        stream.flush()
            finally:
                self.pending.task_done()

    def flush(self) -> None:
        """Drain pending output within the handler shutdown bounds."""
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
        """Release owned resources after active operations have finished."""
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
    """Idempotent process setup, retaining unrelated embedding/test handlers.

    Args:
        settings: Validated runtime settings controlling the resource configuration.
        service: Process service label included in output records.
        non_blocking: Whether to use a bounded background output queue.

    Returns:
        Configured process handler, reusing a compatible existing handler.
    """
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
