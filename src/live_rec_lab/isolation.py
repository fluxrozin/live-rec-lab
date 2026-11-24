"""Filesystem boundary for synthetic development runs."""

import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .db import initialize_database

_TEMP = Path("/tmp")
_MARKER = ".live-rec-lab-fixture"
_LOG_BYTES = 10 * 1024 * 1024
_LOG_FILES = 5
Worker = Literal["server", "speech", "ass", "comments", "collection", "audit"]
_LOG_SECRET = re.compile(
    r"(?i)(\b(?:authorization|cookie|token|client_secret|api[-_ ]?key|sign_api_key)\s*[:=]\s*)([^\r\n]+)"
)
_LOG_URL = re.compile(r"https?://[^\s]+")


class IsolationError(ValueError):
    """The synthetic workspace cannot be opened safely."""


def redact_log_text(value: str) -> str:
    return _LOG_URL.sub("［URL省略］", _LOG_SECRET.sub(r"\1［秘匿］", value))


def check_fixture_directory(path: Path, *, private: bool = False) -> None:
    check_owned_directory(path, private=private)
    if path.stat().st_dev != _TEMP.stat().st_dev:
        raise IsolationError(f"fixture directory must stay on the /tmp filesystem: {path}")


def check_owned_directory(path: Path, *, private: bool = False) -> None:
    details = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(details.st_mode):
        raise IsolationError(f"fixture directory must not be a link: {path}")
    if details.st_uid != os.geteuid() or details.st_mode & forbidden:
        raise IsolationError(f"fixture directory must be owned by this user with safe permissions: {path}")


def _check_location(root: Path) -> None:
    # /tmp's sticky bit prevents other users from replacing the top private directory.
    if not root.is_relative_to(_TEMP) or ".." in root.parts or len(root.relative_to(_TEMP).parts) < 2:
        raise IsolationError("fixture requires a new directory inside a private parent under /tmp")
    for parent in reversed(root.parents):
        if parent != _TEMP and parent.is_relative_to(_TEMP):
            check_fixture_directory(parent)
    check_fixture_directory(root.parent, private=True)


def _check_file_details(details: os.stat_result, path: Path) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o022
    ):
        raise IsolationError(f"fixture file must be an owned regular file without links: {path}")


def _check_file(path: Path, *, optional: bool = False) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        if optional:
            return
        raise
    _check_file_details(details, path)


class Workspace:
    root: Path
    mode: str
    recordings: Path
    database: Path
    events: Path
    lock: Path
    default_port = 8083

    def recheck(self) -> "Workspace":
        raise NotImplementedError

    def check_directory(self, path: Path, *, private: bool = False) -> None:
        check_owned_directory(path, private=private)

    def require_media(self) -> None:
        self.recheck()
        self.require_ready()
        self.check_directory(self.recordings)

    def media_path(self, relative: Path | str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.is_relative_to("recordings") or len(path.parts) < 2:
            raise IsolationError("media path is outside the recording root")
        return self.recordings / path.relative_to("recordings")

    def media_relative(self, path: Path) -> Path:
        relative = Path("recordings") / path.relative_to(self.recordings)
        self.media_path(relative)
        return relative

    @property
    def restore_marker(self) -> Path:
        return self.events.with_name("restore-in-progress.json")

    @property
    def runtime_path(self) -> Path:
        return self.events.parent / "runtime"

    @property
    def backup_path(self) -> Path:
        return self.events.parent / "backups"

    @property
    def restore_pending(self) -> bool:
        return os.path.lexists(self.restore_marker)

    def require_ready(self) -> None:
        if self.restore_pending:
            raise IsolationError("workspace restore is in progress")

    @contextmanager
    def exclusive(self, *, worker: Worker | None = None, maintenance: bool = False) -> Iterator[int]:
        self.recheck()
        path = self.lock.with_name(f"{worker}.lock") if worker else self.lock
        with owned_lock(path) as fd:
            if not maintenance:
                self.require_ready()
            yield fd

    def event(self, component: str, event: str, correlation_id: str, error: Exception | None = None) -> None:
        self.recheck()
        document = {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": component,
            "event": event,
            "correlation_id": correlation_id,
        }
        if error is not None:
            document["exception"] = {"type": type(error).__name__, "message": redact_log_text(str(error))}
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        payload = json.dumps(document, ensure_ascii=False) + "\n"
        with owned_lock(self.events.with_suffix(".lock"), blocking=True):
            _rotate_events(self.events, len(payload.encode()))
            fd = os.open(self.events, flags, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                _check_file_details(os.fstat(fd), self.events)
                stream.write(payload)


def _rotate_events(path: Path, growth: int) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    _check_file_details(info, path)
    if info.st_size + growth <= _LOG_BYTES:
        return
    files = [path, *(path.with_name(f"{path.name}.{i}") for i in range(1, _LOG_FILES))]
    for file in files:
        _check_file(file, optional=True)
    for source, target in reversed(list(zip(files, files[1:], strict=False))):
        if source.exists():
            os.replace(source, target)


@contextmanager
def owned_lock(path: Path, *, blocking: bool = False) -> Iterator[int]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _check_file_details(os.fstat(fd), path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IsolationError("workspace is busy") from error
        yield fd
    finally:
        os.close(fd)


@dataclass(frozen=True)
class FixtureWorkspace(Workspace):
    root: Path
    mode = "fixture"

    @property
    def recordings(self) -> Path:
        return self.root / "recordings"

    @property
    def database(self) -> Path:
        return self.root / "data" / "fixture.sqlite3"

    @property
    def events(self) -> Path:
        return self.root / "state" / "events.jsonl"

    @property
    def lock(self) -> Path:
        return self.root / "state" / "fixture.lock"

    def recheck(self) -> "FixtureWorkspace":
        return self.open(self.root)

    def check_directory(self, path: Path, *, private: bool = False) -> None:
        check_fixture_directory(path, private=private)

    @classmethod
    def initialize(cls, root: Path) -> "FixtureWorkspace":
        _check_location(root)
        root.mkdir(mode=0o700)
        workspace = cls(root)
        for directory in (workspace.recordings, workspace.database.parent, workspace.events.parent):
            directory.mkdir(mode=0o700)
        initialize_database(workspace.database)
        with (root / _MARKER).open("x", encoding="utf-8") as marker:
            json.dump({"root": str(root), "fixture_version": 1}, marker)
        return cls.open(root)

    @classmethod
    def open(cls, root: Path) -> "FixtureWorkspace":
        _check_location(root)
        check_fixture_directory(root, private=True)
        marker = root / _MARKER
        _check_file(marker)
        if json.loads(marker.read_text(encoding="utf-8")) != {"root": str(root), "fixture_version": 1}:
            raise IsolationError("fixture workspace marker does not match the designated directory")
        workspace = cls(root)
        for directory in (workspace.recordings, workspace.database.parent, workspace.events.parent):
            check_fixture_directory(directory, private=True)
        _check_file(workspace.database)
        for path in (workspace.events, workspace.lock, *workspace.database.parent.glob("fixture.sqlite3-*")):
            _check_file(path, optional=True)
        return workspace
