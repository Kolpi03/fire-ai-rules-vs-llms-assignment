"""Structured (JSON-lines) logging.

Every log record is written to ``logs/<name>.jsonl`` as one JSON object and to
stderr in a short human-readable form. Extra fields are passed with
``log.info("msg", extra={"fields": {...}})`` or via :func:`log_event`.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from common.paths import LOG_DIR

_CONFIGURED: set[str] = set()


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a logger writing JSON lines to logs/<top-level-name>.jsonl."""
    logger = logging.getLogger(name)
    top = name.split(".")[0]
    if top not in _CONFIGURED:
        root = logging.getLogger(top)
        root.setLevel(logging.INFO)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_DIR / f"{top}.jsonl", encoding="utf-8")
        fh.setFormatter(JsonLineFormatter())
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
        root.addHandler(sh)
        root.propagate = False
        _CONFIGURED.add(top)
    return logger


def log_event(logger: logging.Logger, msg: str, **fields: Any) -> None:
    logger.info(msg, extra={"fields": fields})
