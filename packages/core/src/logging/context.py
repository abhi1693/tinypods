"""Task-local logging context and monotonic duration measurement."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_context: ContextVar[dict[str, object] | None] = ContextVar("tinypods_log_context", default=None)


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
