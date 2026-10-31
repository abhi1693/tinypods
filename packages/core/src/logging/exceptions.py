"""Complete exception representation and deliberate source-safe boundary diagnostics."""

from __future__ import annotations

import traceback
from typing import NotRequired, TypedDict

from tinypods_core.json_types import JsonRecord, JsonValue
from tinypods_core.logging.values import log_value


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
