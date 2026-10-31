"""Logging contracts, context propagation and stalled collector behavior."""

import asyncio
import importlib.util
import json
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from tinypods_api.main import create_app
from tinypods_core.logging import (
    BaseFormatter,
    JsonFormatter,
    QueuedStderrHandler,
    StderrHandler,
    TextFormatter,
    configure_logging,
    exception_details,
    log_context,
    log_value,
)
from tinypods_core.settings import Settings


def drain() -> None:
    """Flush TinyPods-owned log handlers before captured output is inspected."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, StderrHandler):
            handler.flush()


def records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Parse captured JSON stderr records after draining queued output.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.

    Returns:
        Parsed JSON records captured from stderr.
    """
    drain()
    return [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]


@pytest.mark.parametrize("formatter", [JsonFormatter, TextFormatter])
def test_formats_keep_fields_arguments_and_exceptions(formatter: type[BaseFormatter]) -> None:
    """Verify formats keep fields arguments and exceptions.

    Args:
        formatter: Output formatter class used for the current logging contract case.
    """
    record = logging.LogRecord(
        "tinypods.test", logging.ERROR, __file__, 1, "Failed item %s", ("demo-item",), None
    )
    record.context = {"password": "demo-password", "items": [1, True, None]}
    try:
        raise ValueError("demo failure")
    except ValueError as error:
        record.exc_info = (type(error), error, error.__traceback__)
    rendered = formatter("api").format(record)
    assert "demo-item" in rendered
    if formatter is JsonFormatter:
        assert "demo-password" in rendered
    assert "demo failure" in rendered
    assert "ValueError" in rendered
    assert "[REDACTED]" not in rendered


def test_logging_configuration_is_idempotent_and_keeps_stdout_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify logging configuration is idempotent and keeps stdout clean.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    settings = Settings(log_format="json", log_level="DEBUG")
    foreign = logging.NullHandler()
    logging.getLogger().addHandler(foreign)
    try:
        one = configure_logging(settings, service="api")
        two = configure_logging(settings, service="api")
        assert one is two
        assert foreign in logging.getLogger().handlers
        logging.getLogger("tinypods.test").info("started", extra={"operation_id": "demo"})
        output = capsys.readouterr()
        assert output.out == ""
        assert json.loads(output.err)["operation_id"] == "demo"
        assert logging.getLogger("uvicorn.access").disabled
        assert logging.getLogger("alembic").propagate
        assert importlib.util.find_spec("tinypods_core.log_privacy") is None
    finally:
        logging.getLogger().removeHandler(foreign)


def test_text_defaults_levels_and_dependency_messages(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify text defaults levels and dependency messages.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    configure_logging(Settings(), service="worker")
    logging.getLogger("tinypods.test").debug("hidden_debug")
    logging.getLogger("tinypods.test").info("worker_started")
    logging.getLogger("uvicorn.error").info("Dependency info")
    logging.getLogger("uvicorn.error").warning("Dependency %s", "warning")
    output = capsys.readouterr().err
    assert "worker started" in output
    assert "Dependency warning" in output
    assert "hidden_debug" not in output
    assert "Dependency info" not in output
    assert "[worker]" in output
    assert "location=" not in output


def test_context_nests_and_restores_after_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify context nests and restores after failure.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    configure_logging(Settings(log_format="json"))
    logger = logging.getLogger("tinypods.test")
    with log_context(operation_id="outer"):
        try:
            with log_context(operation_id="inner"):
                logger.info("inside")
                raise ValueError("stop")
        except ValueError:
            logger.info("outside")
    logger.info("cleared")
    assert [record.get("operation_id") for record in records(capsys)] == ["inner", "outer", None]


def test_complete_values_mark_cycles_and_unsupported_objects() -> None:
    """Preserve complete supported data and identify unavoidable JSON representation limits."""

    class Opaque:
        """Refuse arbitrary conversion when a diagnostic value is unsupported."""

        def __str__(self) -> str:
            """Reject object conversion rather than letting the formatter execute hooks.

            Raises:
                AssertionError: Always, if the forbidden conversion is attempted.
            """
            raise AssertionError("object representation requested")

    assert log_value(Opaque())["__tinypods_log_type__"] == "unsupported"
    assert log_value(math.inf) == {"__tinypods_log_type__": "float", "value": "inf"}
    assert log_value(uuid4()) is not None
    assert log_value(datetime.now(UTC)) is not None
    nested: dict[str, object] = {}
    nested["cycle"] = nested
    assert log_value(nested) == {"cycle": {"__tinypods_log_type__": "cycle", "container": "dict"}}
    record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "large_event", (), None)
    for index in range(350):
        setattr(record, f"item_{index}", "x" * 3000)
    payload = json.loads(JsonFormatter("api").format(record))
    assert all(payload[f"item_{index}"] == "x" * 3000 for index in range(350))


def test_exception_chains_keep_stack_locations_without_locals() -> None:
    """Verify exception chains keep stack locations without locals."""
    try:
        try:
            raise KeyError("demo missing item")
        except KeyError as error:
            raise RuntimeError("demo wrapper") from error
    except RuntimeError as error:
        details = exception_details(error)
    assert [detail["type"] for detail in details] == ["RuntimeError", "KeyError"]
    assert details[0]["frames"][0]["file"].endswith("test_logging.py")
    assert set(details[0]["frames"][0]) == {"file", "line", "function"}


def test_queue_never_writes_on_the_event_loop_and_drops_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify queue never writes on the event loop and drops at capacity.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    release = threading.Event()
    lines: list[str] = []
    handler = QueuedStderrHandler(capacity=2)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("tinypods.test", level=logging.INFO)
    logger.addHandler(handler)

    async def scenario() -> None:
        """Exercise bounded logging while its collector stalls."""
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        loop_thread = threading.get_ident()

        class SlowOutput:
            """Simulate a slowly draining collector for bounded flush and close checks."""

            def write(self, line: str) -> None:
                """Exercise a write inside the enclosing transaction or request lifecycle scenario.

                Args:
                    line: Text offered to the deliberately stalled output stream.
                """
                assert threading.get_ident() != loop_thread
                loop.call_soon_threadsafe(started.set)
                assert release.wait(10)
                lines.append(line)

            def flush(self) -> None:
                """Provide the stream flush interface used by the logging handler."""
                pass

        monkeypatch.setattr("sys.stderr", SlowOutput())
        with log_context(request_id="first"):
            logger.info("request_completed")
        await asyncio.wait_for(started.wait(), timeout=1)
        with log_context(request_id="second"):
            for _ in range(3):
                logger.info("request_completed")
        assert handler.pending.qsize() == 2
        assert handler.dropped == 1
        release.set()
        await asyncio.to_thread(handler.flush)
        assert [json.loads(line)["request_id"] for line in lines] == ["first", "second", "second"]

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        handler.close()
        handler.close()
        handler.flush()


def test_formatter_failures_do_not_crash_application(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify formatter failures do not crash application.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    handler = StderrHandler()
    handler.setFormatter(JsonFormatter("api"))
    record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "%d", ("not a number",), None)
    handler.emit(record)
    assert "logging_error" in capsys.readouterr().err
    handler.close()


def test_requests_have_server_ids_and_health_checks_are_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify requests have server ids and health checks are quiet.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    app = create_app(Settings(log_format="json"))

    @app.get("/demo")
    def demo(request: Request) -> dict[str, str]:
        """Return a synthetic response used to inspect correlated request logs.

        Args:
            request: Current synthetic request carrying correlation context.

        Returns:
            Synthetic response fields used by the enclosing assertions.
        """
        logging.getLogger("tinypods.demo").info("inside_request")
        return {"id": str(request.state.request_id)}

    @app.get("/failure")
    async def failure() -> None:
        """Raise a deliberate failure to exercise public error translation."""
        raise ValueError("demo failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        success = client.get("/demo?unused=value", headers={"x-request-id": "client-choice"})
        assert success.json()["id"] == success.headers["x-request-id"]
        UUID(success.headers["x-request-id"])
        assert success.headers["x-request-id"] != "client-choice"
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        failed = client.get("/failure")
        assert failed.status_code == 500
        assert failed.headers["x-request-id"]
    output = records(capsys)
    summaries = [record for record in output if record["event"] == "request_completed"]
    assert [record["route"] for record in summaries] == ["/demo", "/failure"]
    assert summaries[0]["request_id"] == success.headers["x-request-id"]
    assert summaries[1]["status_code"] == 500
    assert any(record["event"] == "request_failed" for record in output)
    assert any(
        record.get("request_id") == success.headers["x-request-id"]
        and record["event"] == "inside_request"
        for record in output
    )
    assert "unused" not in json.dumps(summaries)


def test_concurrent_requests_keep_their_context(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify concurrent requests keep their context.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    app = create_app(Settings(log_format="json"))

    @app.get("/work/{number}")
    async def work(number: int) -> dict[str, int]:
        """Issue a distinct request and return its correlation metadata.

        Args:
            number: Distinct request marker used to detect context leakage.

        Returns:
            Integer fields returned by the synthetic endpoint.
        """
        await asyncio.sleep(0.01)
        logging.getLogger("tinypods.work").info("work_finished", extra={"number": number})
        return {"number": number}

    async def scenario() -> list[httpx.Response]:
        """Run the async lifecycle for concurrent requests keep their context.

        Returns:
            Responses collected from the concurrent request tasks.
        """
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return list(await asyncio.gather(*(client.get(f"/work/{n}") for n in range(8))))

    responses = asyncio.run(scenario())
    completed = {
        record["number"]: record for record in records(capsys) if record["event"] == "work_finished"
    }
    assert len({response.headers["x-request-id"] for response in responses}) == 8
    for number, response in enumerate(responses):
        assert completed[number]["request_id"] == response.headers["x-request-id"]
        assert completed[number]["route"] == f"/work/{number}"


def test_streaming_keeps_context_and_measures_the_body(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify streaming keeps context and measures the body.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    app = create_app(Settings(log_format="json"))

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        """Return a streaming response so logging covers body production.

        Returns:
            Response whose body exposes the test synchronization window.
        """

        async def content() -> AsyncIterator[bytes]:
            """Yield response chunks around the enclosing streaming test synchronization.

            Yields:
                Body chunks emitted around the streaming synchronization points.
            """
            yield b"first"
            await asyncio.sleep(0.02)
            logging.getLogger("tinypods.stream").info("stream_chunk")
            yield b"second"

        return StreamingResponse(content())

    with TestClient(app) as client:
        response = client.get("/stream")
        assert response.content == b"firstsecond"
    output = records(capsys)
    request = next(record for record in output if record["event"] == "request_completed")
    chunk = next(record for record in output if record["event"] == "stream_chunk")
    assert request["request_id"] == chunk["request_id"] == response.headers["x-request-id"]
    assert float(str(request["duration_ms"])) >= 15


def test_cli_json_results_and_logs_use_separate_streams() -> None:
    """Verify cli json results and logs use separate streams."""
    result = subprocess.run(
        ["tinypods", "doctor"],
        env=dict(os.environ, TINYPODS_LOG_FORMAT="json"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["configuration"] == "valid"
    output = [json.loads(line) for line in result.stderr.splitlines()]
    assert [record["event"] for record in output] == ["command_started", "command_completed"]
    assert len({record["command_id"] for record in output}) == 1


def test_queue_flush_and_close_are_bounded_when_output_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify queue flush and close are bounded when output stalls.

    Args:
        monkeypatch: Scoped environment or dependency substitutions restored after the test.
    """
    release, started = threading.Event(), threading.Event()

    class BlockedOutput:
        """Simulate a log collector that stalls while holding its output write."""

        def write(self, line: str) -> None:
            """Exercise a write inside the enclosing transaction or request lifecycle scenario.

            Args:
                line: Text offered to the deliberately stalled output stream.
            """
            started.set()
            release.wait(10)

        def flush(self) -> None:
            """Provide the stream flush interface used by the logging handler."""
            pass

    monkeypatch.setattr("sys.stderr", BlockedOutput())
    handler = QueuedStderrHandler(capacity=1)
    handler.setFormatter(JsonFormatter("api"))
    record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "queued", (), None)
    try:
        handler.emit(record)
        assert started.wait(1)
        handler.emit(record)
        before = time.monotonic()
        handler.flush()
        assert time.monotonic() - before < 1.5
        before = time.monotonic()
        handler.close()
        assert time.monotonic() - before < 1.5
        handler.emit(record)
        handler.flush()
    finally:
        release.set()
        handler.close()
        handler.worker.join(timeout=2)


def test_queued_formatter_failure_and_configuration_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify queued formatter failure and configuration changes.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """
    with pytest.raises(ValueError, match="positive"):
        QueuedStderrHandler(capacity=0)
    first = configure_logging(Settings(log_format="json", log_queue_capacity=1), non_blocking=True)
    broken = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "%d", ("bad",), None)
    first.emit(broken)
    first.flush()
    assert "logging_error" in capsys.readouterr().err
    second = configure_logging(Settings(log_queue_capacity=2), non_blocking=True)
    assert second is not first
    assert isinstance(first, QueuedStderrHandler) and first.stopping.is_set()
    second.close()
    third = configure_logging(Settings(), non_blocking=True)
    assert third is not second
    synchronous = configure_logging(Settings())
    assert not isinstance(synchronous, QueuedStderrHandler)


def test_record_locations_are_json_only_and_exception_frames_are_complete() -> None:
    """Keep ordinary record locations out of text while preserving complete exception frames."""
    record = logging.LogRecord("library.demo", logging.ERROR, __file__, 7, "Demo failure", (), None)
    try:
        raise AttributeError("demo", name="missing", obj=object())
    except AttributeError as error:
        record.exc_info = (type(error), error, error.__traceback__)
    structured = JsonFormatter("api").format(record)
    assert "location" in structured and "frames" in structured
    details = exception_details(record.exc_info[1])
    assert details[0]["attribute"] == "missing"
    for verbose in [False, True]:
        text = TextFormatter("api", verbose=verbose).format(record)
        assert "location=" not in text
        assert "test_logging.py" in text
        assert "frames=" not in text


def test_failed_health_check_is_visible(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify failed health check is visible.

    Args:
        capsys: Captured process stdout and stderr used for output assertions.
    """

    async def unavailable() -> bool:
        """Simulate a failed readiness dependency.

        Returns:
            The simulated dependency availability result.
        """
        return False

    app = create_app(Settings(log_format="json"), readiness_checks={"dependency": unavailable})
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
    summary = next(record for record in records(capsys) if record["event"] == "request_completed")
    assert summary["level"] == "ERROR"
    assert summary["route"] == "/readyz"


@pytest.mark.parametrize("level", [logging.INFO, logging.ERROR])
def test_text_request_summary_avoids_dumping_structured_fields(level: int) -> None:
    """Verify text request summary avoids dumping structured fields.

    Args:
        level: Configured logging threshold for the current case.
    """
    record = logging.LogRecord("tinypods.api", level, __file__, 1, "request_completed", (), None)
    fields = {
        "method": "GET",
        "route": "/apps/demo",
        "status_code": 200,
        "duration_ms": 1500,
        "request_id": "12345678-abcd-1234-abcd-123456789abc",
    }
    for key, value in fields.items():
        setattr(record, key, value)
    text = TextFormatter("api").format(record)
    assert "GET /apps/demo -> 200 (1.50 s" in text
    assert "request_id=" not in text and "method=" not in text and "route=" not in text
    assert ("request 12345678" in text) is (level == logging.ERROR)
    payload = json.loads(JsonFormatter("api").format(record))
    assert all(payload[key] == value for key, value in fields.items())


def test_text_failure_shows_natural_context_and_complete_ids() -> None:
    """Preserve complete identifiers in natural text failure context."""
    record = logging.LogRecord(
        "tinypods.api", logging.ERROR, __file__, 1, "request_failed", (), None
    )
    fields = {
        "method": "POST",
        "route": "/apps",
        "duration_ms": 2.4,
        "request_id": "demo-request",
        "operation_id": "12345678-abcd-1234-abcd-123456789abc",
        "dependency": "database",
    }
    for key, value in fields.items():
        setattr(record, key, value)
    text = TextFormatter("api").format(record)
    assert "Request failed" in text
    for expected in [
        "POST /apps",
        "operation 12345678-abcd-1234-abcd-123456789abc",
        "database",
        "2 ms",
        "request demo-request",
    ]:
        assert expected in text
    assert "request_id=" not in text and "duration_ms=" not in text


def test_dictionary_keys_do_not_invoke_object_string_hooks() -> None:
    """Represent unknown dictionary keys without executing their string conversion.

    Returns:
        None. An explicit mapping envelope preserves the value without calling the key hook.
    """

    class UnsafeKey:
        """Expose a conversion hook that diagnostic formatting should not call."""

        def __str__(self) -> str:
            """Fail if formatting attempts to execute an arbitrary object hook.

            Raises:
                AssertionError: The unsafe string-conversion hook was invoked.
            """
            raise AssertionError("string conversion executed")

    value = log_value({UnsafeKey(): "value"})
    assert value["__tinypods_log_type__"] == "mapping"
    assert value["items"][0][0]["class"].endswith("UnsafeKey")
    assert value["items"][0][1] == "value"


def test_json_preserves_complete_unicode_metadata() -> None:
    """Preserve the entire logger name even when JSON escaping expands its length."""
    name = "🙂" * 20000
    record = logging.LogRecord(name, logging.INFO, "", 1, "large", (), None)
    payload = json.loads(JsonFormatter("api").format(record))
    assert payload["logger"] == name and "truncated" not in payload


def test_cancelled_stream_is_not_logged_as_completed(capsys: pytest.CaptureFixture[str]) -> None:
    """Log cancellation distinctly after response headers have already been sent.

    Args:
        capsys: Captured application log output.

    Returns:
        None. Cancellation propagates and produces a request_cancelled event.
    """
    app = create_app(Settings(log_format="json"))

    async def scenario() -> None:
        """Cancel an in-flight response body after observing its first chunk.

        Returns:
            None. The request task propagates CancelledError.
        """
        started = asyncio.Event()

        @app.get("/cancelled-stream")
        async def endpoint() -> StreamingResponse:
            """Return a response whose body remains open until cancelled.

            Returns:
                Streaming response with a deliberately suspended body iterator.
            """

            async def body() -> AsyncIterator[bytes]:
                """Yield an initial chunk and expose the cancellation window.

                Yields:
                    One body chunk before the test cancels the request.
                """
                yield b"first"
                started.set()
                await asyncio.Event().wait()

            return StreamingResponse(body())

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            request = asyncio.create_task(client.get("/cancelled-stream"))
            await started.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        await asyncio.to_thread(drain)

    asyncio.run(scenario())
    events = [
        record["event"] for record in records(capsys) if record.get("route") == "/cancelled-stream"
    ]
    assert events == ["request_cancelled"]


@pytest.mark.parametrize(
    "metadata",
    [
        {str(i) + "🙂" * 79: 1 for i in range(40)},
        ["🙂" * 2048] * 30,
    ],
)
def test_json_preserves_complete_non_scalar_metadata(metadata: object) -> None:
    """Preserve every collection member supplied as structured metadata.

    Args:
        metadata: Nested diagnostic collection exercising the former fallback path.
    """
    record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "event", (), None)
    with log_context(service=metadata):
        payload = json.loads(JsonFormatter("api").format(record))
    assert payload["service"] == metadata and "truncated" not in payload
