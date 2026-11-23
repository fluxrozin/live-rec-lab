"""Read a bounded snapshot of the application's local JSON Lines logs."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from live_rec_lab.ops.logging_config import redact, redact_fields

# Match the filenames and normal rotation size in configure_logging().
_LOG_NAMES = ("app.jsonl", *(f"app.jsonl.{index}" for index in range(1, 6)))
_MAX_FILE_BYTES = 2_000_000
_MAX_LINE_BYTES = 65_536
_MAX_ROWS_PER_FILE = 10_000
_MAX_PAGE_SIZE = 100
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_ENTRY_FIELDS = frozenset({
    "timestamp", "level", "component", "message", "exception", "exception_type", "exception_message",
    "traceback", "event", "event_code", "correlation_id", "streamer_id", "stream_session_id", "job_id",
    "service", "metadata",
})


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    level: str
    component: str
    message: str
    exception: str = ""
    event: str | None = None
    correlation_id: str | None = None
    streamer_id: int | None = None
    stream_session_id: int | None = None
    job_id: int | str | None = None
    service: str | None = None
    exception_type: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LogResult:
    entries: tuple[LogEntry, ...] = ()
    total: int = 0
    partial: bool = False
    unavailable: bool = False
    oldest: datetime | None = None
    newest: datetime | None = None


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    root, *parts = path.absolute().parts
    # Use verified directory descriptors at every step to keep path checks valid through renames.
    fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for part in parts:
            if part == "..":
                raise OSError("parent directory segments are disallowed")
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        yield fd
    finally:
        os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _read_tail(directory: int, name: str, expected: os.stat_result) -> tuple[list[bytes], bool]:
    if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
        raise OSError("disallowed log file type")

    with os.fdopen(os.open(name, _FILE_FLAGS, dir_fd=directory), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _identity(info) != _identity(expected):
            raise OSError("log file changed during rotation")
        offset = max(0, info.st_size - _MAX_FILE_BYTES)
        source.seek(offset)
        data = source.read(info.st_size - offset)

    partial = bool(offset) or len(data) < info.st_size - offset
    if offset:
        # The first fragment may begin mid-record after applying the byte limit.
        data = data.partition(b"\n")[2]
    lines = data.removesuffix(b"\n").rsplit(b"\n", _MAX_ROWS_PER_FILE)
    if len(lines) > _MAX_ROWS_PER_FILE:
        lines = lines[1:]
        partial = True
    return lines, partial


def _identifier(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def _parse_entry(line: bytes, secrets: tuple[str, ...]) -> LogEntry:
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("log line must be an object")
    timestamp = datetime.fromisoformat(data["timestamp"])
    if timestamp.tzinfo is None:
        raise ValueError("log timestamp must include a timezone")

    def optional_text(key: str, *, identifier: bool = False) -> str | None:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return None
        cleaned = redact(value, secrets)
        return None if identifier and value != cleaned else cleaned

    level, component, message = (data[key] for key in ("level", "component", "message"))
    if not isinstance(level, str) or not level or not isinstance(component, str) or not component:
        raise ValueError("log level and component must be present")
    if not isinstance(message, str):
        raise ValueError("log message must be text")

    metadata = data.get("metadata")
    details = dict(metadata) if isinstance(metadata, dict) else {}
    if metadata is not None and not isinstance(metadata, dict):
        details["metadata"] = metadata
    for key, value in data.items():
        if key not in _ENTRY_FIELDS:
            details.setdefault(key, value)
    parts = []
    for key in ("exception", "exception_message", "traceback"):
        value = redact_fields(data.get(key), secrets)
        if value:
            parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))

    return LogEntry(
        timestamp=timestamp.astimezone(UTC),
        level=redact(level, secrets),
        component=redact(component, secrets),
        message=redact(message, secrets),
        exception="\n".join(dict.fromkeys(parts)),
        event=optional_text("event") or optional_text("event_code"),
        correlation_id=optional_text("correlation_id", identifier=True),
        streamer_id=_identifier(data.get("streamer_id")),
        stream_session_id=_identifier(data.get("stream_session_id")),
        job_id=_identifier(data.get("job_id")) or optional_text("job_id", identifier=True),
        service=optional_text("service"),
        exception_type=optional_text("exception_type"),
        metadata=redact_fields(details, secrets),
    )


def read_logs(
    log_dir: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    level: str = "",
    component: str = "",
    query: str = "",
    correlation_id: str = "",
    streamer_id: int | None = None,
    session_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    secrets: Iterable[str] = (),
) -> LogResult:
    """Return matching rows and the time range of all readable rows in the snapshot."""
    level, component, query = level.casefold(), component.casefold(), query.casefold()
    secrets = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))
    start = start.replace(tzinfo=UTC) if start is not None and start.tzinfo is None else start
    end = end.replace(tzinfo=UTC) if end is not None and end.tzinfo is None else end
    page_size = min(max(1, page_size), _MAX_PAGE_SIZE)
    offset = (max(1, page) - 1) * page_size

    def matches(entry: LogEntry) -> bool:
        identifiers_match = (
            (start is None or entry.timestamp >= start)
            and (end is None or entry.timestamp <= end)
            and (not level or entry.level.casefold() == level)
            and (not component or entry.component.casefold() == component)
            and (not correlation_id or entry.correlation_id == correlation_id)
            and (streamer_id is None or entry.streamer_id == streamer_id)
            and (session_id is None or entry.stream_session_id == session_id)
        )
        if not query or not identifiers_match:
            return identifiers_match
        return any(query in field.casefold() for field in (
            entry.message, entry.exception, entry.exception_type or "", json.dumps(entry.metadata, ensure_ascii=False),
        ))

    partial = read_source = active_missing = False
    oldest = newest = None
    matched: list[tuple[datetime, bytes]] = []
    seen_files: set[tuple[int, int]] = set()
    try:
        with _open_directory(log_dir) as directory:
            for name in _LOG_NAMES:
                try:
                    expected = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    active_missing |= name == _LOG_NAMES[0]
                    continue
                except OSError:
                    partial = True
                    continue
                try:
                    identity = _identity(expected)
                    if identity in seen_files:
                        # A source can move to a rotated slot during this read.
                        partial = True
                        continue
                    lines, limited = _read_tail(directory, name, expected)
                except OSError:
                    partial = True
                    continue
                seen_files.add(identity)
                partial |= limited
                read_source = True
                for line in reversed(lines):
                    if not line:
                        continue
                    if len(line) > _MAX_LINE_BYTES:
                        partial = True
                        continue
                    try:
                        entry = _parse_entry(line, secrets)
                    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
                        partial = True
                        continue
                    oldest = entry.timestamp if oldest is None else min(oldest, entry.timestamp)
                    newest = entry.timestamp if newest is None else max(newest, entry.timestamp)
                    if matches(entry):
                        matched.append((entry.timestamp, line))
    except OSError:
        return LogResult(partial=True, unavailable=True)

    matched.sort(key=lambda item: item[0], reverse=True)
    return LogResult(
        entries=tuple(_parse_entry(raw, secrets) for _, raw in matched[offset : offset + page_size]),
        total=len(matched),
        partial=partial or (active_missing and read_source),
        unavailable=partial and not read_source,
        oldest=oldest,
        newest=newest,
    )
