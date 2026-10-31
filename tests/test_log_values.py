"""Complete diagnostic content, representation limits and formatter parity."""

import asyncio
import json
import logging
import threading
from typing import NoReturn

import pytest
from pydantic import SecretBytes, SecretStr
from tinypods_core.json_types import JsonRecord
from tinypods_core.log_values import encode_json, log_value
from tinypods_core.logging import (
    BaseFormatter,
    JsonFormatter,
    QueuedStderrHandler,
    TextFormatter,
    exception_details,
    log_context,
)


@pytest.mark.parametrize("formatter", [JsonFormatter, TextFormatter])
def test_formatters_share_preparation_and_preserve_full_content(
    formatter: type[BaseFormatter],
) -> None:
    """Keep complete messages and diagnostic collections through either renderer.

    Args:
        formatter: Concrete output renderer sharing BaseFormatter normalization.
    """
    assert formatter.__bases__ == (BaseFormatter,)
    message = "begin\n" + "🙂diagnostic" * 10000 + "END-MESSAGE"
    values = {str(index): [index, "v" * 3000] for index in range(100)}
    record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, message, (), None)
    record.details = values
    with log_context(request_id="full-correlation-identifier"):
        output = formatter("api").format(record)
        payload = formatter("api").payload(record)
    assert payload["message"] == message and payload["details"] == values
    assert "END-MESSAGE" in output and '"99"' in output
    assert "\n" not in output and "\\n" in output
    assert "truncated" not in payload


def test_deep_values_encode_without_python_recursion_limits() -> None:
    """Preserve deeply nested containers without recursive conversion or encoding."""
    value: object = "leaf"
    for _ in range(2500):
        value = [value]
    assert encode_json(log_value(value)) == "[" * 2500 + '"leaf"' + "]" * 2500


def test_mapping_keys_aliases_and_binary_values_are_explicit() -> None:
    """Preserve mapping-key types, repeated aliases and binary content without collisions."""
    shared = [1, 2, 3]
    value = {1: shared, "1": shared, "bytes": b"\x00\xff", "set": frozenset({1, 2})}
    payload = json.loads(encode_json(log_value(value)))
    assert payload["__tinypods_log_type__"] == "mapping"
    assert payload["items"][0] == [1, shared] and payload["items"][1] == ["1", shared]
    assert payload["items"][2][1] == {"__tinypods_log_type__": "bytes", "hex": "00ff"}
    assert sorted(payload["items"][3][1]["items"]) == [1, 2]


@pytest.mark.parametrize(
    "secret", [SecretStr("synthetic-credential"), SecretBytes(b"synthetic-credential")]
)
def test_secret_value_types_keep_their_masked_representation(
    secret: SecretStr | SecretBytes,
) -> None:
    """Retain typed-secret protection without scanning ordinary diagnostic keys or strings.

    Args:
        secret: Synthetic credential wrapped by its explicit secret value type.
    """
    output = encode_json(log_value({"endpoint": secret, "diagnostic": "ordinary full text"}))
    assert "synthetic-credential" not in output
    assert "**********" in output and "ordinary full text" in output


def test_complete_exception_chains_frames_groups_and_cycles() -> None:
    """Preserve long exception graphs and mark cycles without losing other diagnostics."""

    def fail(depth: int) -> NoReturn:
        """Build a long real traceback ending in a complete diagnostic message.

        Args:
            depth: Remaining recursive test calls before the deliberate failure.

        Raises:
            ValueError: Always, at the deepest frame.
        """
        if depth:
            fail(depth - 1)
        raise ValueError("full-message-" * 3000)

    try:
        fail(80)
    except ValueError as error:
        leaf = error
    outer: BaseException = leaf
    for index in range(25):
        wrapper = RuntimeError(f"wrapper-{index}")
        wrapper.__cause__ = outer
        outer = wrapper
    group = ExceptionGroup("diagnostics", [outer, TypeError("second child")])
    details = exception_details(group)
    chains = details[0]["exceptions"]
    assert len(chains[0]) == 26 and chains[1][0]["message"] == "second child"
    assert len(chains[0][-1]["frames"]) > 80
    assert chains[0][-1]["message"] == "full-message-" * 3000
    leaf.__cause__ = outer
    assert exception_details(outer)[-1]["cycle"] is True


@pytest.mark.parametrize("formatter", [JsonFormatter, TextFormatter])
def test_deep_message_arguments_keep_full_content(formatter: type[BaseFormatter]) -> None:
    """Render structured percent-format arguments without the Python repr recursion limit.

    Args:
        formatter: Concrete renderer using the shared message normalization.
    """
    value: object = "END-DEEP-ARGUMENT"
    for _ in range(1500):
        value = [value]
    record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "details %s", (value,), None)
    output = formatter("api").format(record)
    assert "END-DEEP-ARGUMENT" in output and output.count("[") >= 1500


def test_exception_string_failure_preserves_original_arguments() -> None:
    """Expose failed conversion explicitly while retaining all original diagnostic arguments."""

    class BrokenException(Exception):
        """Simulate a third-party exception with a broken display hook."""

        def __str__(self) -> str:
            """Fail conversion deliberately rather than silently losing diagnostic arguments.

            Raises:
                RuntimeError: Always, from the deliberately broken display hook.
            """
            raise RuntimeError("broken hook")

    error = BrokenException({"detail": "x" * 20000})
    details = exception_details(error)
    assert "conversion failed" in details[0]["message"]
    assert details[0]["arguments"] == [{"detail": "x" * 20000}]


def test_queued_rendering_owns_context_and_mutable_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Render on the output thread without observing later context or argument mutations.

    Args:
        capsys: Captured stderr receiving the completed queued JSON record.
    """
    emitting_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()

    class WaitingFormatter(JsonFormatter):
        """Expose the output-thread render window for deterministic mutation checks."""

        def render(self, payload: JsonRecord) -> str:
            """Wait until the producer changes its inputs, then render the owned snapshot.

            Args:
                payload: Normalized data owned by the queued output record.

            Returns:
                Complete JSON rendered on the background output thread.
            """
            assert threading.get_ident() != emitting_thread
            entered.set()
            assert release.wait(timeout=3)
            return super().render(payload)

    async def scenario() -> None:
        """Own handler cleanup while coordinating producer and renderer without blocking I/O."""
        handler = QueuedStderrHandler(capacity=2)
        handler.setFormatter(WaitingFormatter("api"))
        values = ["original"]
        record = logging.LogRecord("tinypods.test", logging.INFO, "", 1, "snapshot", (), None)
        record.details = values
        try:
            with log_context(request_id="complete-request-identifier"):
                handler.emit(record)
            assert await asyncio.to_thread(entered.wait, 2)
            values.append("changed-after-emission")
            release.set()
            await asyncio.to_thread(handler.flush)
        finally:
            release.set()
            await asyncio.to_thread(handler.close)

    asyncio.run(scenario())
    payload = json.loads(capsys.readouterr().err)
    assert payload["details"] == ["original"]
    assert payload["request_id"] == "complete-request-identifier"
