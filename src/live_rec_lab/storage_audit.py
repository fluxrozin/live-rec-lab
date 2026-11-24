"""Bounded, read-only storage checks queued by the operator."""

import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import monotonic

from .db import connect
from .isolation import Workspace
from .media import generation
from .retention import media_deletion_status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_audits (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    status TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS storage_audit_active
    ON storage_audits ((1)) WHERE state IN ('queued', 'running');
"""
_BSIZE = 25
_RESULTS = ("normal", "deleted", "missing", "size_mismatch", "corrupt", "unregistered", "temporary", "unconfirmed")
_MEDIA = {".mp4", ".mkv", ".ts", ".webm", ".srt", ".ass"}
_VIDEO_FORMATS = {".mp4": "mov", ".mkv": "matroska", ".webm": "matroska", ".ts": "mpegts"}
_DISPLAY_LIMIT = 100


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_RESULTS, 0))
    findings: list[dict] = field(default_factory=list)
    checked: int = 0
    registered: int | None = None
    omitted: int = 0
    scope: str = "recordings"

    def add(self, relative: str, status: str, *, registered: bool = False) -> None:
        if registered:
            self.checked += 1
        self.counts[status] += 1
        if status != "normal":
            if len(self.findings) < _DISPLAY_LIMIT:
                self.findings.append({"path": relative, "state": status})
            else:
                self.omitted += 1

    def document(self) -> str:
        return json.dumps(vars(self), ensure_ascii=False)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _reader(workspace: Workspace) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        yield database


def _exists(database: sqlite3.Connection) -> bool:
    return (
        database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'storage_audits'").fetchone()
        is not None
    )


def _public(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    document = dict(row)
    details = json.loads(document.pop("status"))
    return {**document, **details}


def _job(database: sqlite3.Connection, audit_id: int) -> sqlite3.Row:
    row = database.execute("SELECT * FROM storage_audits WHERE id = ?", (audit_id,)).fetchone()
    if row is None:
        raise LookupError("storage audit not found")
    return row


def get_storage_audit(workspace: Workspace, audit_id: int | None = None) -> dict:
    workspace = workspace.recheck()
    workspace.require_ready()
    with _reader(workspace) as database:
        if not _exists(database):
            if audit_id is not None:
                raise LookupError("storage audit not found")
            return {"current": None, "latest": None, "last_completed": None}
        if audit_id is not None:
            return _public(_job(database, audit_id))
        current = database.execute("SELECT * FROM storage_audits WHERE state IN ('queued', 'running')").fetchone()
        latest = database.execute("SELECT * FROM storage_audits ORDER BY id DESC LIMIT 1").fetchone()
        completed = database.execute(
            "SELECT * FROM storage_audits WHERE state = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {"current": _public(current), "latest": _public(latest), "last_completed": _public(completed)}


def enqueue_storage_audit(workspace: Workspace) -> dict:
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        database.executescript(_SCHEMA)
        current = database.execute("SELECT * FROM storage_audits WHERE state IN ('queued', 'running')").fetchone()
        if current is not None:
            return _public(current)
        with database:
            queued = database.execute(
                "INSERT INTO storage_audits (state, status, created_at) VALUES ('queued', ?, ?) RETURNING *",
                (Report().document(), _timestamp()),
            ).fetchone()
        return _public(queued)


def cancel_storage_audit(workspace: Workspace, audit_id: int) -> dict:
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
        if not _exists(database):
            raise LookupError("storage audit not found")
        database.execute(
            """
            UPDATE storage_audits
            SET cancel_requested = 1,
                finished_at = CASE WHEN state = 'queued' THEN ? ELSE finished_at END,
                state = CASE WHEN state = 'queued' THEN 'cancelled' ELSE state END
            WHERE id = ? AND state IN ('queued', 'running')
            """,
            (_timestamp(), audit_id),
        )
        return _public(_job(database, audit_id))


def interrupt_storage_audits(workspace: Workspace) -> None:
    workspace = workspace.recheck()
    with workspace.exclusive(worker="audit"), workspace.exclusive(), closing(connect(workspace.database)) as database:
        if _exists(database):
            with database:
                database.execute(
                    """
                    UPDATE storage_audits SET state = 'interrupted', finished_at = ?
                    WHERE state IN ('queued', 'running')
                    """,
                    (_timestamp(),),
                )


def _claim(workspace: Workspace) -> int | None:
    with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
        if not _exists(database):
            return None
        claimed = database.execute(
            "UPDATE storage_audits SET state = 'running', started_at = ? WHERE state = 'queued' RETURNING id",
            (_timestamp(),),
        ).fetchone()
        return claimed["id"] if claimed else None


def _progress(workspace: Workspace, audit_id: int, report: Report, state: str = "running") -> dict:
    with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
        row = _job(database, audit_id)
        if row["cancel_requested"]:
            state = "cancelled"
        updated = database.execute(
            "UPDATE storage_audits SET status = ?, state = ?, finished_at = ? WHERE id = ? RETURNING *",
            (report.document(), state, None if state == "running" else _timestamp(), audit_id),
        ).fetchone()
        return _public(updated)


def _root_identity(workspace: Workspace) -> tuple[int, int]:
    root = workspace.recordings
    workspace.require_media()
    if root.resolve(strict=True) != root:
        raise ValueError("audit recording root is not canonical")
    details = root.stat()
    return details.st_dev, details.st_ino


def _inventory(workspace: Workspace) -> tuple[int, int]:
    with _reader(workspace) as database:
        return tuple(
            database.execute(
                """
            SELECT coalesce(max(id), 0), count(*) + (SELECT count(*) FROM subtitle_publications)
            FROM recording_segments
            """
            ).fetchone()
        )


def _registered_batch(workspace: Workspace, last: int, highest: int) -> list[tuple[int, list[tuple[str, str, str]]]]:
    with _reader(workspace) as database:
        segments = database.execute(
            """
            SELECT segment.*, session.stream_state, session.recording_state
            FROM recording_segments AS segment
            JOIN stream_sessions AS session ON session.id = segment.session_id
            WHERE segment.id > ? AND segment.id <= ? ORDER BY segment.id LIMIT ?
            """,
            (last, highest, _BSIZE),
        ).fetchall()
        result = []
        for segment in segments:
            deletion = media_deletion_status(database, segment["session_id"])
            if segment["recording_state"] == "deleted":
                state = "deleted"
            elif deletion or segment["recording_state"] in {"running", "needs_review", "interrupted"}:
                state = "unconfirmed"
            else:
                state = "video"
            paths = [(segment["video_path"], segment["video_generation"], state)]
            for subtitle in database.execute(
                "SELECT relative_path, file_generation FROM subtitle_publications WHERE segment_id = ?",
                (segment["id"],),
            ):
                paths.append((*subtitle, "subtitle" if state == "video" else state))
            result.append((segment["id"], paths))
        return result


def _video_state(path: Path, expected: str) -> str:
    format_name = _VIDEO_FORMATS.get(path.suffix.casefold())
    if format_name is None:
        return "unconfirmed"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        current = os.fstat(fd)
        identity = [current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns]
        if not stat.S_ISREG(current.st_mode) or identity != json.loads(expected):
            return "unconfirmed"
        result = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-f",
                format_name,
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                f"/proc/self/fd/{fd}",
            ],
            timeout=2,
            check=False,
            capture_output=True,
            pass_fds=(fd,),
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
        return "normal" if result.returncode == 0 and result.stdout.strip() == b"video" else "corrupt"
    finally:
        os.close(fd)


def _registered_state(workspace: Workspace, relative: str, expected: str, state: str) -> str:
    if state in {"deleted", "unconfirmed"}:
        return state
    try:
        target = workspace.media_path(relative)
        if target.resolve(strict=True) != target:
            return "unconfirmed"
        actual = generation(target)
        if json.loads(actual)[2] != json.loads(expected)[2]:
            return "size_mismatch"
        if actual != expected:
            return "unconfirmed"
        result = _video_state(target, expected) if state == "video" else "normal"
        return result if generation(target) == actual else "unconfirmed"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unconfirmed"


def _belongs_to_current_inventory(workspace: Workspace, path: str) -> bool:
    with _reader(workspace) as database:
        return (
            database.execute(
                """
            SELECT 1 FROM recording_segments AS segment
            JOIN stream_sessions AS session ON session.id = segment.session_id
            WHERE segment.video_path = ? AND session.recording_state != 'deleted'
            UNION
            SELECT 1 FROM subtitle_publications AS subtitle
            JOIN recording_segments AS segment ON segment.id = subtitle.segment_id
            JOIN stream_sessions AS session ON session.id = segment.session_id
            WHERE subtitle.relative_path = ? AND session.recording_state != 'deleted'
            LIMIT 1
            """,
                (path, path),
            ).fetchone()
            is not None
        )


def _filesystem_entries(workspace: Workspace) -> Iterator[tuple[str, str]]:
    directories = [workspace.recordings]
    while directories:
        directory = directories.pop()
        try:
            if directory.resolve(strict=True) != directory:
                raise ValueError("audit directory has symbolic alias")
            workspace.check_directory(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = str(workspace.media_relative(path))
                    if entry.is_symlink():
                        yield relative, "unconfirmed"
                    elif entry.is_dir(follow_symlinks=False):
                        directories.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if (
                            ".part" in entry.name
                            or entry.name.startswith((".subtitle-stage-", ".subtitle-previous-"))
                            or directory.name.startswith(".live-rec-delete-")
                        ):
                            yield relative, "temporary"
                        elif path.suffix.casefold() in _MEDIA:
                            yield relative, "unregistered"
                    yield "", ""
        except (OSError, ValueError):
            relative = str(workspace.media_relative(directory)) if directory != workspace.recordings else "recordings"
            yield relative, "unconfirmed"


def _scan(workspace: Workspace, audit_id: int, report: Report, deadline: float, stop: Event | None) -> dict:
    baseline = _root_identity(workspace)
    highest, report.registered = _inventory(workspace)
    checked_paths = set()
    last = 0
    while monotonic() < deadline:
        if stop and stop.is_set():
            return _progress(workspace, audit_id, report, "interrupted")
        if _root_identity(workspace) != baseline:
            raise ValueError("audit recording root changed")
        batch = _registered_batch(workspace, last, highest)
        if not batch:
            break
        for last, paths in batch:
            for relative, expected, state in paths:
                if stop and stop.is_set():
                    return _progress(workspace, audit_id, report, "interrupted")
                if monotonic() >= deadline:
                    return _progress(workspace, audit_id, report, "partial")
                if state != "deleted":
                    checked_paths.add(relative)
                report.add(relative, _registered_state(workspace, relative, expected, state), registered=True)
        current = _progress(workspace, audit_id, report)
        if current["state"] != "running":
            return current
    for index, (relative, state) in enumerate(_filesystem_entries(workspace)):
        if stop and stop.is_set():
            return _progress(workspace, audit_id, report, "interrupted")
        if monotonic() >= deadline:
            return _progress(workspace, audit_id, report, "partial")
        if index % 100 == 0:
            if _root_identity(workspace) != baseline:
                raise ValueError("audit recording root changed")
            with _reader(workspace) as database:
                if _job(database, audit_id)["cancel_requested"]:
                    return _progress(workspace, audit_id, report, "cancelled")
        if not relative or relative in checked_paths:
            continue
        if state == "unregistered" and _belongs_to_current_inventory(workspace, relative):
            state = "unconfirmed"
        report.add(relative, state)
    if _root_identity(workspace) != baseline:
        raise ValueError("audit recording root changed")
    state = "partial" if report.counts["unconfirmed"] or report.checked != report.registered else "completed"
    return _progress(workspace, audit_id, report, state)


def run_storage_audit(workspace: Workspace, *, timeout: float = 30, stop: Event | None = None) -> dict | None:
    """Run one pending scan on a background worker, never on a GET request."""
    workspace = workspace.recheck()
    with workspace.exclusive(worker="audit"):
        audit_id = _claim(workspace)
        if audit_id is None:
            return None
        report = Report()
        try:
            result = _scan(workspace, audit_id, report, monotonic() + timeout, stop)
        except (OSError, ValueError, sqlite3.Error) as error:
            workspace.event("storage", "audit_failed", str(audit_id), error)
            result = _progress(workspace, audit_id, report, "partial" if report.checked else "failed")
        else:
            workspace.event("storage", f"audit_{result['state']}", str(audit_id))
        return result
