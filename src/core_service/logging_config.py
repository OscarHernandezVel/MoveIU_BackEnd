"""Structured JSON logging with automatic redaction of sensitive data."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

SENSITIVE_KEYS = ("password", "token", "secret", "authorization", "api_key", "apikey", "email", "phone", "key")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BEARER = re.compile(r"(?i)bearer\s+[\w\-.=+/]+")
_STANDARD_ATTRS = set(vars(logging.LogRecord("x", 0, "x", 0, "", (), None))) | {"message", "asctime"}


def redact(text: str) -> str:
    """Mask emails and bearer tokens inside free text."""
    return _BEARER.sub("Bearer [REDACTED]", _EMAIL.sub("[REDACTED_EMAIL]", text))


def redact_value(key: str, value: Any) -> Any:
    if any(marker in key.lower() for marker in SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS:
                payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
