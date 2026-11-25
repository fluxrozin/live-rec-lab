"""構造化ログ（JSON Lines、10MiB×5世代、秘匿）。"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from live_rec_lab.config import ensure_private_dir

_SECRET_VALUES: set[str] = set()
_URL_QUERY = re.compile(r"(https?://[^\s\"'?]+)\?[^\s\"']+")
_AUTH = re.compile(r"(?i)(authorization|api[_-]?key|secret|token|signature|x-api-key)([\"'=: ]+)([^\s\"',}]+)")


def register_secrets(values) -> None:
    for v in values:
        if v and len(v) >= 6:
            _SECRET_VALUES.add(v)


def redact(text: str) -> str:
    for s in _SECRET_VALUES:
        text = text.replace(s, "***")
    text = _URL_QUERY.sub(r"\1?***", text)
    return _AUTH.sub(r"\1\2***", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "source": record.name,
            "message": redact(record.getMessage()),
            "event_id": getattr(record, "event_id", None) or uuid.uuid4().hex[:12],
        }
        for k in ("session_id", "streamer_id", "job_id", "instance_id"):
            if getattr(record, k, None) is not None:
                data[k] = getattr(record, k)
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path, level: str = "INFO", *, console: bool = True) -> None:
    ensure_private_dir(log_dir)
    root = logging.getLogger("live_rec_lab")
    root.handlers.clear()
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=4,
                                              encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    os.chmod(log_dir / "app.log", 0o600)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)
    root.propagate = False
