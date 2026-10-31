"""Immutable protocol values owned by backend logging."""

from enum import StrEnum
from typing import Final


class LogEvent(StrEnum):
    """Stable event identifiers shared by emitters and output renderers."""

    REQUEST_COMPLETED = "request_completed"
    REQUEST_CANCELLED = "request_cancelled"
    REQUEST_FAILED = "request_failed"
    COMMAND_STARTED = "command_started"
    COMMAND_COMPLETED = "command_completed"
    COMMAND_FAILED = "command_failed"
    DEPENDENCY = "dependency_log"


class LogField(StrEnum):
    """Stable structured field names shared across emitters and renderers."""

    EVENT = "event"
    LEVEL = "level"
    MESSAGE = "message"
    SERVICE = "service"
    REQUEST_ID = "request_id"
    COMMAND_ID = "command_id"
    METHOD = "method"
    ROUTE = "route"
    STATUS = "status_code"
    DURATION = "duration_ms"


CONTEXT_IDENTIFIERS: Final[tuple[tuple[str, str], ...]] = (
    ("operation_id", "operation"),
    ("job_id", "job"),
    ("app_id", "app"),
    ("cluster_id", "cluster"),
)
QUIET_COMMAND_EVENTS: Final = frozenset({LogEvent.COMMAND_STARTED, LogEvent.COMMAND_COMPLETED})
ERROR_LEVELS: Final = frozenset({"WARNING", "ERROR", "CRITICAL"})
TEXT_CONTEXT_FIELDS: Final = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "service",
        "event",
        "pid",
        "location",
        "message",
        "exception",
        "method",
        "route",
        "status_code",
        "duration_ms",
        "request_id",
        "operation_id",
        "job_id",
        "app_id",
        "cluster_id",
        "dependency",
    }
)
