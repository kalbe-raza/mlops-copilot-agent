"""Structured JSON logging with secret redaction.

Every tool invocation produces one JSON line containing the request id, tool name, status and
duration. Values whose key looks sensitive (token, password, api_key, secret, authorization ...)
and any literal secret value registered at start-up are replaced with ``***REDACTED***``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"
SENSITIVE_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)",
                           re.IGNORECASE)
INLINE_SECRET = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}|(AIza[0-9A-Za-z_\-]{20,})")

_secret_values: set[str] = set()


def register_secrets(values: list[str]) -> None:
    for value in values:
        if value and len(value) >= 4:
            _secret_values.add(value)


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = value
        for secret in _secret_values:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return INLINE_SECRET.sub(lambda m: (m.group(1) or "") + REDACTED, text)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


def get_logger(log_file: Path | None = None, console: bool = False) -> logging.Logger:
    logger = logging.getLogger("mlops_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = True  # lets pytest's caplog observe records
    if getattr(logger, "_configured", False):
        return logger
    formatter = JsonFormatter()
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        logger.addHandler(sh)
    logger._configured = True  # type: ignore[attr-defined]
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": redact(fields)})
