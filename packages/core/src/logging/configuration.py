"""Process logging configuration and bounded lifecycle cleanup."""

from __future__ import annotations

import logging

from tinypods_core.logging.formatters import JsonFormatter, TextFormatter
from tinypods_core.logging.handlers import QueuedStderrHandler, StderrHandler
from tinypods_core.settings import Settings


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
