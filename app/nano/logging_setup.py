"""Structured JSON logging for Cloud Run, with request correlation.

The original service had no application logging at all — `severity>=WARNING`
returned nothing across its entire life — which is why two production bugs stayed
mis-diagnosed until someone queried the tables directly. Every log line here
carries the WhatsApp message id and the Cloud Trace id, so a single message can
be followed across the webhook and its background task.

Phone numbers are redacted. The repository is going public and the logs are read
in screenshots.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from typing import Any

# Per-request context, attached to every log record emitted while handling it.
_message_id: contextvars.ContextVar[str] = contextvars.ContextVar("message_id", default="")
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

# Cloud Logging severities that differ from Python's names.
_SEVERITY = {
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
}


def redact_number(number: str | None) -> str:
    """`15550100001` -> `***0001`. Enough to correlate, not enough to dial."""
    if not number:
        return "unknown"
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"


class CloudRunJsonFormatter(logging.Formatter):
    """Emit the JSON shape Cloud Logging parses into structured fields."""

    def __init__(self, project_id: str = ""):
        super().__init__()
        self.project_id = project_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelname, record.levelname),
            "message": record.getMessage(),
            "logger": record.name,
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        if message_id := _message_id.get():
            payload["messageId"] = message_id

        if (trace := _trace_id.get()) and self.project_id:
            payload["logging.googleapis.com/trace"] = f"projects/{self.project_id}/traces/{trace}"

        # Anything passed via logger.info("...", extra={"foo": 1}) becomes a field.
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(project_id: str = "", level: str = "") -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudRunJsonFormatter(project_id=project_id))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn duplicates access logs that Cloud Run already records.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").propagate = True


def bind_request(message_id: str = "", trace_header: str = "") -> None:
    """Attach identifiers to the current request context."""
    if message_id:
        _message_id.set(message_id)
    if trace_header:
        # Header form: TRACE_ID/SPAN_ID;o=1
        _trace_id.set(trace_header.split("/")[0])


def clear_request() -> None:
    _message_id.set("")
    _trace_id.set("")


def log_with(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log with structured fields: log_with(logger, logging.INFO, "x", items=3)."""
    logger.log(level, message, extra={"extra_fields": fields})
