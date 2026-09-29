"""Structured JSON logging with correlation IDs and secret redaction.

Every record carries ``txn_id`` (from a contextvar set by the orchestrator) and ``mode``,
so a single purchase can be traced across parsing, policy, approval and execution.
A redaction filter scrubs anything that looks like a key, bearer token or card number
before a record is emitted - defence in depth on top of never logging those values.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

current_txn: contextvars.ContextVar[str | None] = contextvars.ContextVar("txn_id", default=None)
current_mode: contextvars.ContextVar[str | None] = contextvars.ContextVar("mode", default=None)

_PATTERNS = [
    (re.compile(r"\b(sk|rk|pk)_(live|test)_[A-Za-z0-9]{6,}"), r"\1_\2_[REDACTED]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{6,}"), "sk-ant-[REDACTED]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"\bspt_[A-Za-z0-9]{6,}"), "spt_[REDACTED]"),
    # 13-19 digit sequences, optionally separated - possible PANs.
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[REDACTED_PAN]"),
    (re.compile(r"(?i)(cvv|cvc)\W{0,3}\d{3,4}"), r"\1 [REDACTED]"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "txn_id": current_txn.get(),
            "mode": current_mode.get(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("spenddat")
    root.setLevel(level)
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"spenddat.{name}")


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
