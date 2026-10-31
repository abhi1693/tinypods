"""Complete JSON-safe diagnostics with explicit cycle and unsupported-value markers."""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import SecretBytes, SecretStr
from tinypods_core.json_types import JsonValue

TYPE_MARKER: Final = "__tinypods_log_type__"
ITEMS_MARKER: Final = "items"


@dataclass(frozen=True)
class PendingValue:
    """Assign one normalized value into its parent without recursive Python calls."""

    value: object
    parent: list[JsonValue] | dict[str, JsonValue]
    key: int | str


@dataclass(frozen=True)
class FinishedContainer:
    """Remove an ancestor identity after its children have been normalized."""

    identity: int


@dataclass(frozen=True)
class JsonToken:
    """Distinguish JSON punctuation from strings that still require escaping."""

    text: str


def assign(task: PendingValue, value: JsonValue) -> None:
    """Store a normalized value in the parent slot owned by a pending task.

    Args:
        task: Parent container and exact destination slot.
        value: Complete JSON-compatible diagnostic value.
    """
    if isinstance(task.parent, list):
        assert isinstance(task.key, int)
        task.parent[task.key] = value
    else:
        assert isinstance(task.key, str)
        task.parent[task.key] = value


def log_value(value: object) -> JsonValue:
    """Normalize complete supported values without size, depth or item-count limits.

    Strings and string-keyed dictionaries retain their full content. Tuples become
    JSON arrays. Non-string-keyed mappings and sets use explicit typed envelopes,
    preserving values without colliding JSON object keys. Cycles and unsupported
    objects are marked explicitly; arbitrary object conversion hooks are not run.
    SecretStr and SecretBytes retain their masked display rather than credentials.

    Args:
        value: Diagnostic value captured for a log record.

    Returns:
        Complete JSON-compatible data, with representation markers where JSON
        cannot directly express the original Python value or reference graph.
    """
    root: list[JsonValue] = [None]
    pending: list[PendingValue | FinishedContainer] = [PendingValue(value, root, 0)]
    ancestors: set[int] = set()
    while pending:
        task = pending.pop()
        if isinstance(task, FinishedContainer):
            ancestors.remove(task.identity)
            continue
        item = task.value
        if isinstance(item, (SecretStr, SecretBytes)):
            assign(task, str(item))
        elif item is None or isinstance(item, (str, bool, int)):
            assign(task, item)
        elif isinstance(item, float):
            assign(
                task, item if math.isfinite(item) else {TYPE_MARKER: "float", "value": str(item)}
            )
        elif isinstance(item, (UUID, datetime)):
            assign(task, str(item))
        elif isinstance(item, bytes):
            assign(task, {TYPE_MARKER: "bytes", "hex": item.hex()})
        elif isinstance(item, (dict, list, tuple, set, frozenset)):
            identity = id(item)
            if identity in ancestors:
                assign(task, {TYPE_MARKER: "cycle", "container": type(item).__name__})
                continue
            ancestors.add(identity)
            pending.append(FinishedContainer(identity))
            if isinstance(item, dict) and all(isinstance(key, str) for key in item):
                mapping: dict[str, JsonValue] = {}
                assign(task, mapping)
                pending.extend(
                    PendingValue(child, mapping, key)
                    for key, child in reversed(tuple(item.items()))
                )
            elif isinstance(item, dict):
                pairs: list[JsonValue] = []
                assign(task, {TYPE_MARKER: "mapping", ITEMS_MARKER: pairs})
                children: list[PendingValue] = []
                for key, child in item.items():
                    pair: list[JsonValue] = [None, None]
                    pairs.append(pair)
                    children.extend((PendingValue(key, pair, 0), PendingValue(child, pair, 1)))
                pending.extend(reversed(children))
            else:
                sequence = tuple(item)
                values: list[JsonValue] = [None] * len(sequence)
                assign(
                    task,
                    {TYPE_MARKER: type(item).__name__, ITEMS_MARKER: values}
                    if isinstance(item, (set, frozenset))
                    else values,
                )
                pending.extend(
                    PendingValue(child, values, index)
                    for index, child in reversed(tuple(enumerate(sequence)))
                )
        else:
            assign(task, {TYPE_MARKER: "unsupported", "class": type(item).__qualname__})
    return root[0]


def encode_json(value: JsonValue) -> str:
    """Encode complete normalized JSON without recursive-container depth limits.

    Args:
        value: Normalized acyclic JSON-compatible value.

    Returns:
        A complete single-line JSON representation with escaped control characters.
    """
    parts: list[str] = []
    pending: list[JsonValue | JsonToken] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, JsonToken):
            parts.append(item.text)
        elif isinstance(item, dict):
            parts.append("{")
            pending.append(JsonToken("}"))
            for index, (key, child) in reversed(tuple(enumerate(item.items()))):
                pending.extend(
                    (child, JsonToken(": "), JsonToken(json.dumps(key, ensure_ascii=True)))
                )
                if index:
                    pending.append(JsonToken(", "))
        elif isinstance(item, list):
            parts.append("[")
            pending.append(JsonToken("]"))
            for index, child in reversed(tuple(enumerate(item))):
                pending.append(child)
                if index:
                    pending.append(JsonToken(", "))
        else:
            parts.append(json.dumps(item, ensure_ascii=True, allow_nan=False))
    return "".join(parts)


@dataclass(frozen=True)
class MessageArgument:
    """Render structured percent-format arguments without recursive Python repr calls."""

    value: JsonValue

    def __str__(self) -> str:
        """Return the complete JSON representation for percent-s formatting.

        Returns:
            Escaped structured data without nesting or size truncation.
        """
        return encode_json(self.value)

    def __repr__(self) -> str:
        """Return the complete JSON representation for percent-r formatting.

        Returns:
            Escaped structured data without nesting or size truncation.
        """
        return encode_json(self.value)


def message_argument(value: JsonValue) -> object:
    """Keep numeric/string format semantics and wrap structured values for deep rendering.

    Args:
        value: Normalized argument supplied to the standard percent-format convention.

    Returns:
        The scalar itself or a nonrecursive string/repr adapter for a container.
    """
    return MessageArgument(value) if isinstance(value, (list, dict)) else value
