"""JSON process logging with scoped redaction and no exception payloads."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from tinypods_core.settings import Settings

_SENSITIVE = ("password", "secret", "token", "authorization", "cookie", "credential")


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = secrets

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]"
                if any(x in str(key).lower() for x in _SENSITIVE)
                else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        return value

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redact(record.getMessage()),
        }
        if hasattr(record, "context"):
            payload["context"] = self.redact(record.context)
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=lambda _: "[UNSUPPORTED]")


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.secret_values()))
    logger = logging.getLogger("tinypods")
    logger.handlers[:] = [handler]
    logger.setLevel(settings.log_level)
    logger.propagate = False
