"""Synchronous and queued stderr output with owned prepared snapshots."""

from __future__ import annotations

import logging
import queue
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

from tinypods_core.json_types import JsonRecord
from tinypods_core.logging.formatters import BaseFormatter, TextFormatter

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
