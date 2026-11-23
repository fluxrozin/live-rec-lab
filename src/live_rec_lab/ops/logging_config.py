"""JSON Lines の運用ログと、記録・閲覧で共有する秘匿処理。"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

_URL = re.compile(r"\b(?:https?|wss?|rtmps?|srt)://[^\s<>\"']+", re.IGNORECASE)
_SECRET_KEY = (
    r"(?:authorization|proxy[-_]authorization|bearer|(?:x[-_])?(?:sign[-_])?api[-_]?key|"
    r"(?:twitcasting[-_])?client[-_]?(?:id|secret)|password|secret|"
    r"(?:access[-_]|refresh[-_]|auth[-_]|session[-_])?token|signature|cookie|set-cookie|"
    r"stream[-_]url|media[-_]url|hls[-_]url|websocket[-_]url)"
)
_SENSITIVE_KEY = re.compile(_SECRET_KEY, re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(?P<key>\b" + _SECRET_KEY + r"['\"]?\s*[:=][ \t]*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?:(?:basic|bearer)[ \t]+)?[^\s,;}&]+)",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"\bbearer[ \t]+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_COOKIE_HEADER = re.compile(r"(?P<key>\b(?:set-cookie|cookie)[ \t]*:[ \t]*)[^\r\n]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_configured = False


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = _URL.sub("[url]", text)
    cleaned = _COOKIE_HEADER.sub(r"\g<key>[secret]", cleaned)
    cleaned = _SENSITIVE_VALUE.sub(r"\g<key>[secret]", cleaned)
    cleaned = _BEARER_TOKEN.sub("[secret]", cleaned)
    cleaned = _JWT.sub("[secret]", cleaned)
    configured = {
        os.environ.get(name, "") for name in ("TWITCASTING_CLIENT_SECRET", "TWITCASTING_CLIENT_ID", "SIGN_API_KEY")
    }
    for secret in sorted((configured | set(secrets)) - {""}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, "[secret]")
    return cleaned.encode("utf-8", errors="replace").decode("utf-8")


def redact_fields(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Mask field values before serializing, so JSON syntax and nearby fields remain intact."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            cleaned = "[secret]" if _SENSITIVE_KEY.fullmatch(name) else redact_fields(item, secrets)
            result[redact(name, secrets)] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [redact_fields(item, secrets) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value), secrets)


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "component": record.name,
            "event": str(getattr(record, "event", record.funcName)),
            "message": record.getMessage(),
        }
        for key in ("correlation_id", "job_id", "stream_session_id", "service", "streamer_id", "metadata"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            if record.exc_info[0] is not None:
                payload["exception_type"] = record.exc_info[0].__name__
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact_fields(payload, self.secrets), ensure_ascii=False)


class _PrivateLogHandler(RotatingFileHandler):
    def _open(self) -> TextIO:
        descriptor = os.open(
            self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("ログの実体を確認できません")
            return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(descriptor)
            raise


def set_log_level(level: str) -> None:
    logging.getLogger("live_rec_lab").setLevel(level)


def configure_logging(log_dir: Path, level: str = "INFO") -> None:
    from live_rec_lab.config import load_secrets

    global _configured
    set_log_level(level)
    if _configured:
        return
    log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    handler = _PrivateLogHandler(
        log_dir / "app.jsonl",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    credentials = load_secrets()
    handler.setFormatter(JsonFormatter((
        credentials.twitcasting_client_id, credentials.twitcasting_client_secret, credentials.sign_api_key,
    )))
    root = logging.getLogger("live_rec_lab")
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def reset_logging() -> None:
    global _configured
    root = logging.getLogger("live_rec_lab")
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    _configured = False
