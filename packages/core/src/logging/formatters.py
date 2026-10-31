"""Shared record preparation and complete JSON/text rendering."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import cast

from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.logging.constants import (
    QUIET_COMMAND_EVENTS,
    TEXT_CONTEXT_FIELDS,
    LogEvent,
    LogField,
)
from tinypods_core.logging.context import _context
from tinypods_core.logging.exceptions import exception_details
from tinypods_core.logging.text import context_text, error_text, event_text, inline, local_time
from tinypods_core.logging.values import encode_json, log_value, message_argument

_EVENT = re.compile(r"[a-z][a-z0-9_]{0,100}\Z")


_APP_LOGGERS = ("tinypods.", "tinypods_core.", "tinypods_api.", "tinypods_worker.", "tinypods_cli.")


_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {LogField.MESSAGE, "asctime"}


_RESERVED_FIELDS = {"timestamp", LogField.LEVEL, "logger", LogField.EVENT, "pid", "exception"}


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
