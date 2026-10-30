"""DevFeed-style human summaries of structured operational events."""

import json
import re
from datetime import datetime

from tinypods_core.json_types import JsonRecord, JsonValue


def inline(value: JsonValue) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)[1:-1]
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def short_id(value: JsonValue) -> str:
    text = str(value)
    return text[:8] if re.fullmatch(r"[0-9a-f-]{32,64}", text) else inline(value)[:40]


def local_time(value: JsonValue) -> str:
    return datetime.fromisoformat(str(value)).astimezone().strftime("%H:%M:%S")


def event_text(payload: JsonRecord) -> str:
    if payload["event"] == "request_completed":
        return (
            f"{inline(payload.get('method', 'HTTP'))} "
            f"{inline(payload.get('route', '<unmatched>'))} -> {payload.get('status_code', '?')}"
        )
    if payload["event"] == "request_failed":
        return "Request failed"
    return inline(payload.get("message") or str(payload["event"]).replace("_", " "))


def error_text(payload: JsonRecord) -> str:
    details = payload.get("exception")
    if not isinstance(details, list):
        return ""
    return inline(
        "; ".join(
            f"{item.get('type')}: {item.get('message')}"
            for item in details
            if isinstance(item, dict)
        )
    )


def context_text(payload: JsonRecord) -> list[str]:
    parts: list[str] = []
    if payload.get("route") and payload["event"] != "request_completed":
        parts.append(f"{inline(payload.get('method', 'HTTP'))} {inline(payload['route'])}")
    for key, label in [
        ("operation_id", "operation"),
        ("job_id", "job"),
        ("app_id", "app"),
        ("cluster_id", "cluster"),
    ]:
        if payload.get(key):
            parts.append(f"{label} {short_id(payload[key])}")
            break
    if payload.get("dependency"):
        parts.append(inline(payload["dependency"]))
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        parts.append(f"{duration:.0f} ms" if duration < 1000 else f"{duration / 1000:.2f} s")
    if payload.get("request_id") and payload["level"] in {"WARNING", "ERROR", "CRITICAL"}:
        parts.append(f"request {short_id(payload['request_id'])}")
    return parts
