"""JSON structured logging with automatic secret redaction (plan §7).

Every record becomes one JSON line: ts (UTC ISO), level, logger, msg + any `extra` fields (signal_id, trade_id,
cl_ord_id, ...). The RedactingFilter masks: the exact secret values registered at startup (API key/secret,
Telegram token, healthcheck URL), API-Sign / API-Key header values, Telegram bot-token patterns and long base64
blobs (>= 40 chars), in the message, the args and every string extra."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = ["JsonFormatter", "RedactingFilter", "configure_logging", "redact", "register_secret"]

_STD = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None)).keys()) | {"message", "asctime"}
_PATTERNS = [
    re.compile(r"(API-(?:Sign|Key)['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.I),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),          # Telegram bot token
    re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),                  # long base64 (Kraken secret, signatures)
]
_RESERVED = frozenset({"ts", "level", "logger", "msg", "exc"})
_secrets: list[str] = []


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6 and value not in _secrets:
        _secrets.append(value)


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        text = text.replace(s, "***")
    text = _PATTERNS[0].sub(lambda m: m.group(1) + "***", text)
    for p in _PATTERNS[1:]:
        text = p.sub("***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(redact(str(a)) for a in args)
        for k, v in list(vars(record).items()):
            if k not in _STD and isinstance(v, str):
                setattr(record, k, redact(v))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        for k, v in vars(record).items():
            if k not in _STD and not k.startswith("_"):
                out[f"x_{k}" if k in _RESERVED else k] = v  # an `extra` can never overwrite ts/level/logger/msg
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return redact(json.dumps(out, default=str, ensure_ascii=False))


def configure_logging(level: str = "INFO", secrets: list[str | None] | None = None) -> None:
    for s in secrets or []:
        register_secret(s)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    h.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs at INFO (Telegram URL has the token)
