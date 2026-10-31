"""Human-readable summaries of structured operational events."""

import json
from datetime import datetime

from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.log_constants import CONTEXT_IDENTIFIERS, ERROR_LEVELS, LogEvent, LogField
from tinypods_core.log_values import encode_json


def inline(value: JsonValue) -> str:
    """Escape JSON-compatible diagnostic data for a single-line text log.

    Args:
        value: JSON-compatible diagnostic value to escape.

    Returns:
        Escaped text with control characters represented rather than emitted.
    """
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)[1:-1]
    return encode_json(value)


def identifier_text(value: JsonValue) -> str:
    """Preserve full correlation identifiers while escaping control characters.

    Args:
        value: Correlation identifier or other JSON-compatible label.

    Returns:
        Complete escaped identifier or label.
    """
    return inline(value)


def local_time(value: JsonValue) -> str:
    """Render an ISO timestamp in the process local timezone.

    Args:
        value: ISO-formatted event timestamp.

    Returns:
        Local hours, minutes and seconds in HH:MM:SS format.
    """
    return datetime.fromisoformat(str(value)).astimezone().strftime("%H:%M:%S")


def event_text(payload: JsonRecord) -> str:
    """Translate structured operational events into compact human summaries.

    Args:
        payload: Structured event fields used to compose the text summary.

    Returns:
        Request summary, known failure label or escaped application message.
    """
    if payload[LogField.EVENT] == LogEvent.REQUEST_COMPLETED:
        return (
            f"{inline(payload.get(LogField.METHOD, 'HTTP'))} "
            f"{inline(payload.get(LogField.ROUTE, '<unmatched>'))} -> "
            f"{payload.get(LogField.STATUS, '?')}"
        )
    if payload[LogField.EVENT] == LogEvent.REQUEST_CANCELLED:
        return "Request cancelled"
    if payload[LogField.EVENT] == LogEvent.REQUEST_FAILED:
        return "Request failed"
    return inline(payload.get(LogField.MESSAGE) or str(payload[LogField.EVENT]).replace("_", " "))


def error_text(payload: JsonRecord) -> str:
    """Summarize structured exception messages for the text formatter.

    Args:
        payload: Structured event fields used to compose the text summary.

    Returns:
        Joined exception types and messages, or an empty string without exception details.
    """
    details = payload.get("exception")
    if not isinstance(details, list):
        return ""
    # Include every chain frame and group child, not just exception summaries.
    return inline(details)


def context_text(payload: JsonRecord) -> list[str]:
    """Select useful operational context without repeating the request summary.

    Args:
        payload: Structured event fields used to compose the text summary.

    Returns:
        Selected human-readable context fragments.
    """
    parts: list[str] = []
    if payload.get(LogField.ROUTE) and payload[LogField.EVENT] != LogEvent.REQUEST_COMPLETED:
        parts.append(
            f"{inline(payload.get(LogField.METHOD, 'HTTP'))} {inline(payload[LogField.ROUTE])}"
        )
    for key, label in CONTEXT_IDENTIFIERS:
        if payload.get(key):
            parts.append(f"{label} {identifier_text(payload[key])}")
    if payload.get("dependency"):
        parts.append(inline(payload["dependency"]))
    duration = payload.get(LogField.DURATION)
    if isinstance(duration, (int, float)):
        parts.append(f"{duration:.0f} ms" if duration < 1000 else f"{duration / 1000:.2f} s")
    if payload.get(LogField.REQUEST_ID) and payload[LogField.LEVEL] in ERROR_LEVELS:
        parts.append(f"request {identifier_text(payload[LogField.REQUEST_ID])}")
    return parts
