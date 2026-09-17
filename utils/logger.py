"""Application logging with optional redacted JSON event output."""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import colorlog

from config import settings


class RedactingFilter(logging.Filter):
    _patterns = (
        (re.compile(r"\b\d{6,}:\S+"), "[REDACTED_TELEGRAM_TOKEN]"),
        (re.compile(r"api\.telegram\.org/file/bot[^/\s]+", re.IGNORECASE), "api.telegram.org/file/[REDACTED]"),
        (re.compile(r"X-Telegram-Init-Data[=:]\s*[^\s]+", re.IGNORECASE), "X-Telegram-Init-Data=[REDACTED]"),
        (re.compile(r"\b\d{12,19}\b"), "[REDACTED_NUMBER]"),
        (re.compile(r"\+7\d{10}\b"), "[REDACTED_PHONE]"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern, replacement in self._patterns:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "event_fields", None)
        if fields:
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logger(name: str = "bot", level: int | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    configured_level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    logger.setLevel(configured_level if level is None else level)
    logger.propagate = False
    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logger.level)
    console_handler.addFilter(RedactingFilter())
    if settings.LOG_FORMAT == "json":
        console_handler.setFormatter(JsonFormatter())
    else:
        console_handler.setFormatter(
            colorlog.ColoredFormatter(
                "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s: %(message)s",
                datefmt="%H:%M:%S",
                log_colors={
                    "DEBUG": "cyan",
                    "INFO": "green",
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "bold_red",
                },
            )
        )
    logger.addHandler(console_handler)
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    *,
    component: str,
    event: str,
    outcome: str,
    reason: str = "",
    order_id: int | None = None,
    attempt_id: int | None = None,
    error_type: str = "",
) -> None:
    """Log only allowlisted operational metadata."""
    fields = {
        "component": component,
        "event": event,
        "outcome": outcome,
        "reason": reason,
        "order_id": order_id,
        "attempt_id": attempt_id,
        "error_type": error_type,
    }
    logger.log(level, "%s.%s: %s", component, event, outcome, extra={"event_fields": fields})
