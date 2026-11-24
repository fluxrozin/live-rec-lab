"""Filesystem boundary for synthetic development runs."""

import fcntl
import json
import os
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


class IsolationError(ValueError):
    """The synthetic workspace cannot be opened safely."""


def check_fixture_directory(path: Path, *, private: bool = False) -> None:
    details = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(details.st_mode):
        raise IsolationError(f"fixture directory must not be a link: {path}")
    if details.st_uid != os.geteuid() or details.st_mode & forbidden:
        raise IsolationError(f"fixture directory must be owned by this user with safe permissions: {path}")
    if details.st_dev != _TEMP.stat().st_dev:
        raise IsolationError(f"fixture directory must stay on the /tmp filesystem: {path}")


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


@dataclass(frozen=True)
class FixtureWorkspace:
    root: Path

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

    @contextmanager
    def exclusive(self, *, worker: Literal["server", "speech", "comments"] | None = None) -> Iterator[int]:
        self.open(self.root)
        lock = self.lock.with_name(f"{worker}.lock") if worker else self.lock
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            _check_file_details(os.fstat(fd), lock)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise IsolationError("fixture workspace is busy") from error
            yield fd
        finally:
            os.close(fd)

    def event(self, component: str, event: str, correlation_id: str, error: Exception | None = None) -> None:
        self.open(self.root)
        document = {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": component,
            "event": event,
            "correlation_id": correlation_id,
        }
        if error is not None:
            document["exception"] = {"type": type(error).__name__, "message": str(error)}
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.events, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            _check_file_details(os.fstat(stream.fileno()), self.events)
            stream.write(json.dumps(document, ensure_ascii=False) + "\n")
