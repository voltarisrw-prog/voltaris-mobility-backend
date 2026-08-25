"""Structured JSON logging with request correlation.

`request_id` is stored in a ContextVar rather than threaded through call sites, so a
log line emitted five layers deep in a repository still carries the id of the request
that caused it. That is the difference between a usable production log and a pile of
disconnected messages.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

#: Never let these appear in a log line, whatever a caller passes as `extra`.
REDACTED_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "secret",
    "webhook_secret",
    "signature",
    "card",
    "cvv",
}

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _redact(value: Any, key: str = "") -> Any:
    if key.lower() in REDACTED_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        user_id = user_id_var.get()
        if user_id:
            payload["user_id"] = user_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = _redact(value, key)
        if record.exc_info:
            # Full traceback goes to the log, never to the client.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn duplicates access logs in its own format; ours carries the request id.
    logging.getLogger("uvicorn.access").disabled = True
