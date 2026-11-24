"""Generation-checked, resumable deletion of managed recording media."""

import ctypes
import json
import os
import sqlite3
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .db import connect
from .isolation import Workspace

DeletionMode = Literal["files", "files_and_history"]
_OperationMode = DeletionMode | Literal["comments"]

_SUFFIXES = {"comment": "_comment.srt", "speech": "_speech.srt", "ass": ".ass"}
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_deletion_operations (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    streamer_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('files', 'files_and_history', 'comments')),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'needs_review', 'completed')),
    recordings_root TEXT NOT NULL,
    root_device INTEGER NOT NULL,
    root_inode INTEGER NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    previous_expiration TEXT,
    previous_recording_failures INTEGER NOT NULL,
    last_segment_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS media_deletion_session
    ON media_deletion_operations (session_id) WHERE mode != 'comments';
CREATE UNIQUE INDEX IF NOT EXISTS media_deleted_source
    ON media_deletion_operations (streamer_id, source_id) WHERE mode != 'comments';
CREATE UNIQUE INDEX IF NOT EXISTS comment_deletion_session
    ON media_deletion_operations (session_id) WHERE mode = 'comments';
CREATE TABLE IF NOT EXISTS media_deletion_targets (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES media_deletion_operations(id),
    segment_id INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    file_generation TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'captured', 'removed', 'absent')),
    captured_generation TEXT,
    quarantine_device INTEGER,
    quarantine_inode INTEGER,
    UNIQUE (operation_id, relative_path)
);
"""
_SETTLED_RECORDINGS = {"completed", "partial", "cancelled", "deleted"}
_NO_RECORDING = {"failed", "not_started", "disabled", "skipped", "blocked"}
_SESSION_END_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_settlements (
    session_id INTEGER PRIMARY KEY REFERENCES stream_sessions(id) ON DELETE CASCADE,
    ended_at TEXT NOT NULL
)
"""


class MediaDeletionConflict(ValueError):
    """Deletion could not safely proceed for this session."""


@dataclass(frozen=True)
class MediaRetentionPolicy:
    enabled: bool = False
    keep_days: int = 0
    maximum_bytes: int = 0
    minimum_free_bytes: int = 5 * 1024**3

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.keep_days) is not int or not 0 <= self.keep_days <= 36500:
            raise ValueError("invalid automatic retention policy")
        if any(type(value) is not int or value < 0 for value in (self.maximum_bytes, self.minimum_free_bytes)):
            raise ValueError("invalid storage byte limit")


def record_session_end(database: sqlite3.Connection, session_id: int, ended_at: datetime) -> None:
    if ended_at.utcoffset() is None:
        raise ValueError("confirmed end time must include its time zone")
    database.execute(_SESSION_END_SCHEMA)
    database.execute(
        "INSERT INTO session_settlements VALUES (?, ?) ON CONFLICT(session_id) DO NOTHING",
        (session_id, ended_at.astimezone(UTC).isoformat()),
    )


def _table(database: sqlite3.Connection, name: str) -> bool:
    return (
        database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
        is not None
    )


def _operation(database: sqlite3.Connection, session_id: int, *, comments: bool = False) -> sqlite3.Row | None:
    if not _table(database, "media_deletion_operations"):
        return None
    condition = "mode = 'comments'" if comments else "mode != 'comments'"
    return database.execute(
        f"SELECT * FROM media_deletion_operations WHERE session_id = ? AND {condition}", (session_id,)
    ).fetchone()


def media_deletion_status(database: sqlite3.Connection, session_id: int) -> dict | None:
    operation = _operation(database, session_id)
    if operation is None:
        return None
    return _operation_status(database, operation)


def _operation_status(database: sqlite3.Connection, operation: sqlite3.Row) -> dict:
    counts = database.execute(
        """
        SELECT count(*) FILTER (WHERE state IN ('pending', 'captured')),
               count(*) FILTER (WHERE state = 'removed')
        FROM media_deletion_targets WHERE operation_id = ?
        """,
        (operation["id"],),
    ).fetchone()
    return {
        "operation_id": operation["id"],
        "session_id": operation["session_id"],
        "mode": operation["mode"],
        "state": operation["state"],
        "files": {"pending": counts[0], "removed": counts[1]},
        "started_at": operation["started_at"],
        "finished_at": operation["finished_at"],
    }


def deleted_source(database: sqlite3.Connection, streamer_id: int, source_id: str) -> bool:
    if not _table(database, "media_deletion_operations"):
        return False
    return (
        database.execute(
            """
        SELECT 1 FROM media_deletion_operations WHERE streamer_id = ? AND source_id = ? AND mode != 'comments'
        """,
            (streamer_id, source_id),
        ).fetchone()
        is not None
    )


def next_session_id(database: sqlite3.Connection) -> int:
    active = database.execute("SELECT coalesce(max(id), 0) FROM stream_sessions").fetchone()[0]
    deleted = (
        database.execute("SELECT coalesce(max(session_id), 0) FROM media_deletion_operations").fetchone()[0]
        if _table(database, "media_deletion_operations")
        else 0
    )
    return max(active, deleted) + 1


def next_segment_id(database: sqlite3.Connection) -> int:
    active = database.execute("SELECT coalesce(max(id), 0) FROM recording_segments").fetchone()[0]
    deleted = (
        database.execute("SELECT coalesce(max(segment_id), 0) FROM media_deletion_targets").fetchone()[0]
        if _table(database, "media_deletion_targets")
        else 0
    )
    return max(active, deleted) + 1


def _generation(details: os.stat_result) -> str:
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid():
        raise MediaDeletionConflict("deletion target is not an owned, unlinked regular file")
    return json.dumps([details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns])


def _identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _file_generation(directory: int, name: str) -> str | None:
    equivalents = [entry for entry in os.listdir(directory) if _name_key(entry) == _name_key(name)]
    if equivalents and equivalents != [name]:
        raise MediaDeletionConflict("deletion target name has a case or Unicode collision")
    try:
        return _generation(os.stat(name, dir_fd=directory, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _root_descriptor(workspace: Workspace) -> int:
    workspace.require_media()
    if workspace.mode == "normal":
        # The verified Windows publication method does not establish support
        # for the exclusive rename used by the deletion journal.
        raise MediaDeletionConflict("exclusive media deletion is unverified on the Windows recording root")
    if workspace.recordings.resolve(strict=True) != workspace.recordings:
        raise MediaDeletionConflict("recordings root is not in canonical form")
    return os.open(workspace.recordings, _DIRECTORY)


@contextmanager
def _media_directory(database: sqlite3.Connection, root: int, relative_path: str) -> Iterator[tuple[int, str]]:
    parts = Path(relative_path).parts
    if len(parts) < 3 or parts[0] != "recordings" or ".." in parts or str(Path(relative_path)) != relative_path:
        raise MediaDeletionConflict("deletion path is outside a managed recordings directory")
    descriptor = os.dup(root)
    try:
        for depth, part in enumerate(parts[1:-1], start=2):
            expected = database.execute(
                "SELECT device, inode FROM media_directories WHERE relative_path = ?",
                (str(Path(*parts[:depth])),),
            ).fetchone()
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            found = os.fstat(descriptor)
            if not expected or tuple(expected) != _identity(found) or found.st_dev != os.fstat(root).st_dev:
                raise MediaDeletionConflict("managed media directory identity changed")
            if found.st_uid != os.geteuid() or found.st_mode & 0o022:
                raise MediaDeletionConflict("managed media directory permits outside writes")
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _assert_settled(database: sqlite3.Connection, session_id: int, *, comments: bool = False) -> sqlite3.Row:
    session = database.execute("SELECT * FROM stream_sessions WHERE id = ?", (session_id,)).fetchone()
    if (
        session is None
        or session["stream_state"] != "ended"
        or session["recording_state"] not in _SETTLED_RECORDINGS | _NO_RECORDING
        or session["comment_state"] == "running"
    ):
        raise MediaDeletionConflict("deletion requires an ended session with settled recording and comments")
    checks = (
        ("speech_attempts", "state IN ('queued', 'running')"),
        ("ass_attempts", "state IN ('queued', 'running')"),
        (
            "subtitle_revision_operations",
            "state = 'prepared' AND kind != 'speech'" if comments else "state = 'prepared'",
        ),
    )
    # Subtitle jobs that are only rendering commit under this same writer lock
    # and recheck expiration before publication. A prepared file swap must settle.
    for table, condition in checks[-1:] if comments else checks:
        if (
            _table(database, table)
            and database.execute(
                f"SELECT 1 FROM {table} WHERE {condition} AND segment_id IN "
                "(SELECT id FROM recording_segments WHERE session_id = ?) LIMIT 1",
                (session_id,),
            ).fetchone()
        ):
            raise MediaDeletionConflict("deletion is blocked by pending subtitle work")
    expiration = database.execute(
        "SELECT state FROM comment_expirations WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not comments and expiration is not None and expiration["state"] != "expired":
        raise MediaDeletionConflict("deletion is blocked by an unfinished comment expiration")
    if (
        _table(database, "provider_recording_attempts")
        and database.execute(
            "SELECT 1 FROM provider_recording_attempts WHERE session_id = ? AND state IN ('running', 'needs_review')",
            (session_id,),
        ).fetchone()
    ):
        raise MediaDeletionConflict("deletion is blocked by unfinished recording work")
    return session


def _segment_targets(
    database: sqlite3.Connection, segment: sqlite3.Row, mode: _OperationMode
) -> list[tuple[str, str | None]]:
    video = Path(segment["video_path"])
    subtitles = database.execute(
        "SELECT kind, relative_path, file_generation FROM subtitle_publications WHERE segment_id = ?", (segment["id"],)
    ).fetchall()
    recorded = {row["kind"]: (row["relative_path"], row["file_generation"]) for row in subtitles}
    expected = []
    for kind, suffix in _SUFFIXES.items():
        path = str(video.with_name(video.stem + suffix))
        expected_path, generation = recorded.pop(kind, (path, None))
        if mode == "comments" and kind == "speech":
            continue
        if expected_path != path:
            raise MediaDeletionConflict("subtitle is outside its reserved media name")
        expected.append((path, generation))
    if recorded:
        raise MediaDeletionConflict("deletion found a managed subtitle of unknown kind")
    if mode != "comments":
        expected.append((segment["video_path"], segment["video_generation"]))
    return expected


def _subtitle_artifacts(
    database: sqlite3.Connection, segment: sqlite3.Row, mode: _OperationMode
) -> list[tuple[str, str]]:
    if not _table(database, "subtitle_revision_operations"):
        return []
    rows = database.execute(
        """
        SELECT kind, staged_path, staged_generation, backup_path, backup_generation
        FROM subtitle_revision_operations WHERE segment_id = ?
        """,
        (segment["id"],),
    ).fetchall()
    artifacts = {}
    directory = Path(segment["video_path"]).parent
    for row in rows:
        if mode == "comments" and row["kind"] == "speech":
            continue
        for role, prefix in (("staged", ".subtitle-stage-"), ("backup", ".subtitle-previous-")):
            relative, expected = row[f"{role}_path"], row[f"{role}_generation"]
            if relative is None and expected is None:
                continue
            if (
                not relative
                or not expected
                or Path(relative).parent != directory
                or not Path(relative).name.startswith(prefix)
                or (relative in artifacts and artifacts[relative] != expected)
            ):
                raise MediaDeletionConflict("subtitle staging file identity is unconfirmed")
            artifacts[relative] = expected
    return list(artifacts.items())


def _prepare(
    database: sqlite3.Connection,
    workspace: Workspace,
    root: int | None,
    session: sqlite3.Row,
    mode: _OperationMode,
) -> sqlite3.Row:
    segments = database.execute(
        "SELECT * FROM recording_segments WHERE session_id = ? ORDER BY number", (session["id"],)
    ).fetchall()
    if not segments and (
        session["recording_state"] in {"completed", "partial"}
        or (mode != "comments" and session["recording_state"] in {"failed", "cancelled"})
    ):
        raise MediaDeletionConflict("recording metadata is incomplete")
    targets = []
    if root is not None:
        for segment in segments:
            if mode == "comments":
                with _media_directory(database, root, segment["video_path"]) as (directory, name):
                    if _file_generation(directory, name) != segment["video_generation"]:
                        raise MediaDeletionConflict("video generation is unconfirmed for comment expiration")
            for path, expected in _segment_targets(database, segment, mode):
                with _media_directory(database, root, path) as (directory, name):
                    if _file_generation(directory, name) != expected:
                        raise MediaDeletionConflict("deletion target generation is unconfirmed")
                targets.append((segment["id"], path, expected, "pending" if expected else "absent"))
            for path, expected in _subtitle_artifacts(database, segment, mode):
                with _media_directory(database, root, path) as (directory, name):
                    observed = _file_generation(directory, name)
                    if observed is not None and observed != expected:
                        raise MediaDeletionConflict("subtitle staging file generation is unconfirmed")
                targets.append((segment["id"], path, observed, "pending" if observed else "absent"))
    expiration = database.execute("SELECT * FROM comment_expirations WHERE session_id = ?", (session["id"],)).fetchone()
    failures = (
        database.execute(
            "SELECT attempts FROM provider_recording_failures WHERE session_id = ?", (session["id"],)
        ).fetchone()
        if _table(database, "provider_recording_failures")
        else None
    )
    with database:
        if mode == "comments":
            database.execute(
                """
                INSERT INTO comment_expirations (session_id, state, started_at) VALUES (?, 'running', ?)
                ON CONFLICT(session_id) DO UPDATE SET state = 'running'
                """,
                (session["id"], datetime.now(UTC).isoformat()),
            )
        operation = database.execute(
            """
            INSERT INTO media_deletion_operations (
                session_id, streamer_id, source_id, mode, state, recordings_root, root_device, root_inode, nonce,
                previous_expiration, previous_recording_failures, last_segment_number, started_at
            ) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *
            """,
            (
                session["id"],
                session["streamer_id"],
                session["source_id"],
                mode,
                str(workspace.recordings),
                *(_identity(os.fstat(root)) if root is not None else (0, 0)),
                uuid4().hex,
                json.dumps(dict(expiration)) if expiration else None,
                failures[0] if failures else 0,
                max((segment["number"] for segment in segments), default=0),
                datetime.now(UTC).isoformat(),
            ),
        ).fetchone()
        database.executemany(
            """
            INSERT INTO media_deletion_targets (operation_id, segment_id, relative_path, file_generation, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(operation["id"], *target) for target in targets],
        )
    return operation


@contextmanager
def _quarantine(
    database: sqlite3.Connection, directory: int, operation: sqlite3.Row, target: sqlite3.Row
) -> Iterator[int]:
    name = f".live-rec-delete-{operation['nonce']}-{target['id']}"
    try:
        descriptor = os.open(name, _DIRECTORY, dir_fd=directory)
    except FileNotFoundError:
        if target["quarantine_inode"] is not None:
            raise MediaDeletionConflict("prepared deletion directory is missing") from None
        os.mkdir(name, 0o700, dir_fd=directory)
        os.fsync(directory)
        descriptor = os.open(name, _DIRECTORY, dir_fd=directory)
    try:
        details = os.fstat(descriptor)
        if details.st_uid != os.geteuid() or details.st_mode & 0o077:
            raise MediaDeletionConflict("deletion quarantine is not private")
        identity = _identity(details)
        if target["quarantine_inode"] is None:
            if os.listdir(descriptor):
                raise MediaDeletionConflict("unregistered deletion quarantine is not empty")
            with database:
                database.execute(
                    "UPDATE media_deletion_targets SET quarantine_device = ?, quarantine_inode = ? WHERE id = ?",
                    (*identity, target["id"]),
                )
        elif identity != (target["quarantine_device"], target["quarantine_inode"]):
            raise MediaDeletionConflict("prepared deletion directory identity changed")
        yield descriptor
    finally:
        os.close(descriptor)


def _move_no_replace(source_fd: int, source: str, target_fd: int, target: str) -> None:
    # Python's ordinary rename can overwrite an unexpected destination.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise MediaDeletionConflict("exclusive local quarantine move is unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:  # RENAME_NOREPLACE
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _unlink_captured(directory: int) -> None:
    os.unlink("file", dir_fd=directory)
    os.fsync(directory)


def _capture(
    database: sqlite3.Connection,
    directory: int,
    name: str,
    quarantine: int,
    target: sqlite3.Row,
) -> str:
    captured = _file_generation(quarantine, "file")
    if captured is None:
        if _file_generation(directory, name) != target["file_generation"]:
            raise MediaDeletionConflict("pending deletion source generation changed")
        _move_no_replace(directory, name, quarantine, "file")
        captured = _file_generation(quarantine, "file")
    if captured is None or json.loads(captured)[:-1] != json.loads(target["file_generation"])[:-1]:
        # The source changed between inspection and rename. Preserve the captured
        # object, restoring its original name only if that name is still free.
        if captured is not None and _file_generation(directory, name) is None:
            _move_no_replace(quarantine, "file", directory, name)
            os.fsync(directory)
            os.fsync(quarantine)
        raise MediaDeletionConflict("the source was replaced during deletion preparation")
    os.fsync(directory)
    os.fsync(quarantine)
    with database:
        database.execute(
            "UPDATE media_deletion_targets SET state = 'captured', captured_generation = ? WHERE id = ?",
            (captured, target["id"]),
        )
    return captured


def _clean_empty_quarantine(directory: int, descriptor: int, name: str) -> None:
    try:
        found = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(found) == _identity(os.fstat(descriptor)) and not os.listdir(descriptor):
            os.rmdir(name, dir_fd=directory)
            os.fsync(directory)
    except OSError:
        pass  # Empty owned staging directories do not affect the recorded outcome.


def _remove_target(database: sqlite3.Connection, root: int, operation: sqlite3.Row, target: sqlite3.Row) -> None:
    with _media_directory(database, root, target["relative_path"]) as (directory, name):
        if target["state"] in {"absent", "removed"}:
            if _file_generation(directory, name) is not None:
                raise MediaDeletionConflict("a new file occupies a deletion target")
            return
        with _quarantine(database, directory, operation, target) as quarantine:
            expected = (
                _capture(database, directory, name, quarantine, target)
                if target["state"] == "pending"
                else target["captured_generation"]
            )
            if _file_generation(directory, name) is not None:
                raise MediaDeletionConflict("a new file occupies a captured deletion target")
            captured = _file_generation(quarantine, "file")
            if captured is not None:
                if captured != expected:
                    raise MediaDeletionConflict("captured deletion target generation changed")
                _unlink_captured(quarantine)
            else:
                os.fsync(quarantine)
            # A durable captured state and an unchanged quarantine distinguish an
            # interrupted unlink from a source file that was never managed.
            with database:
                database.execute("UPDATE media_deletion_targets SET state = 'removed' WHERE id = ?", (target["id"],))
            _clean_empty_quarantine(directory, quarantine, f".live-rec-delete-{operation['nonce']}-{target['id']}")


def _complete(database: sqlite3.Connection, operation: sqlite3.Row) -> None:
    session_id = operation["session_id"]
    with database:
        if operation["mode"] == "comments":
            _complete_comment_expiration(database, session_id)
        elif operation["mode"] == "files_and_history":
            expiration = database.execute(
                "SELECT * FROM comment_expirations WHERE session_id = ?", (session_id,)
            ).fetchone()
            if expiration is not None:
                database.execute(
                    "UPDATE media_deletion_operations SET previous_expiration = ? WHERE id = ?",
                    (json.dumps(dict(expiration)), operation["id"]),
                )
            database.execute(
                """
                DELETE FROM subtitle_publications WHERE segment_id IN (
                    SELECT id FROM recording_segments WHERE session_id = ?
                )
                """,
                (session_id,),
            )
            database.execute("DELETE FROM recording_segments WHERE session_id = ?", (session_id,))
            database.execute("DELETE FROM comments WHERE session_id = ?", (session_id,))
            database.execute("DELETE FROM comment_expirations WHERE session_id = ?", (session_id,))
            database.execute("DELETE FROM stream_sessions WHERE id = ?", (session_id,))
        else:
            database.execute(
                """
                UPDATE stream_sessions SET recording_state = 'deleted',
                    subtitle_state = CASE WHEN subtitle_state = 'expired' THEN 'expired' ELSE 'deleted' END
                WHERE id = ?
                """,
                (session_id,),
            )
        database.execute(
            "UPDATE media_deletion_operations SET state = 'completed', finished_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), operation["id"]),
        )


def _complete_comment_expiration(database: sqlite3.Connection, session_id: int) -> None:
    removed = database.execute("DELETE FROM comments WHERE session_id = ?", (session_id,)).rowcount
    database.execute(
        """
        DELETE FROM subtitle_publications WHERE kind IN ('comment', 'ass')
        AND segment_id IN (SELECT id FROM recording_segments WHERE session_id = ?)
        """,
        (session_id,),
    )
    database.execute(
        "UPDATE stream_sessions SET comment_state = 'expired', subtitle_state = 'expired' WHERE id = ?",
        (session_id,),
    )
    database.execute(
        """
        UPDATE comment_expirations SET state = 'expired', completed_at = ?, removed_comments = ?
        WHERE session_id = ?
        """,
        (datetime.now(UTC).isoformat(), removed, session_id),
    )


def _resume(database: sqlite3.Connection, workspace: Workspace, root: int | None, operation: sqlite3.Row) -> dict:
    session_id = operation["session_id"]
    comments = operation["mode"] == "comments"
    try:
        session = _assert_settled(database, session_id, comments=comments)
        if (session["streamer_id"], session["source_id"]) != (operation["streamer_id"], operation["source_id"]):
            raise MediaDeletionConflict("recording session identity changed")
        identity = _identity(os.fstat(root)) if root is not None else (0, 0)
        if str(workspace.recordings) != operation["recordings_root"] or identity != (
            operation["root_device"],
            operation["root_inode"],
        ):
            raise MediaDeletionConflict("prepared recording root identity changed")
        targets = database.execute(
            "SELECT * FROM media_deletion_targets WHERE operation_id = ? ORDER BY id", (operation["id"],)
        ).fetchall()
        if targets and root is None:
            raise MediaDeletionConflict("prepared deletion requires an available recording root")
        for target in targets:
            assert root is not None
            _remove_target(database, root, operation, target)
        for target in targets:
            assert root is not None
            with _media_directory(database, root, target["relative_path"]) as (directory, name):
                if _file_generation(directory, name) is not None:
                    raise MediaDeletionConflict("a new file occupies a completed deletion target")
        _complete(database, operation)
    except (OSError, ValueError, sqlite3.Error) as error:
        with database:
            database.execute(
                "UPDATE media_deletion_operations SET state = 'needs_review' WHERE id = ?", (operation["id"],)
            )
            if comments:
                database.execute(
                    "UPDATE comment_expirations SET state = 'needs_review' WHERE session_id = ?", (session_id,)
                )
        workspace.event("retention", "media_deletion_needs_review", str(operation["id"]), error)
    else:
        workspace.event("retention", "media_deletion_completed", str(operation["id"]))
    current = _operation(database, session_id, comments=comments)
    assert current is not None
    return _operation_status(database, current)


def delete_session_media(workspace: Workspace, session_id: int, mode: DeletionMode) -> dict:
    if mode not in {"files", "files_and_history"}:
        raise ValueError("unknown media deletion mode")
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        operation = _operation(database, session_id)
        if (
            operation
            and operation["state"] == "completed"
            and (operation["mode"] == mode or operation["mode"] == "files_and_history")
        ):
            report = media_deletion_status(database, session_id)
            assert report is not None
            return report
        root = _root_descriptor(workspace)
        try:
            if operation is None:
                session = _assert_settled(database, session_id)
                database.executescript(_SCHEMA)
                operation = _prepare(database, workspace, root, session, mode)
            elif operation["mode"] != mode:
                if operation["state"] != "completed":
                    raise MediaDeletionConflict("finish the prepared deletion before changing its mode")
                _assert_settled(database, session_id)
                with database:
                    database.execute(
                        """
                        UPDATE media_deletion_operations SET mode = ?, state = 'prepared', finished_at = NULL
                        WHERE id = ?
                        """,
                        (mode, operation["id"]),
                    )
                operation = _operation(database, session_id)
                assert operation is not None
            return _resume(database, workspace, root, operation)
        finally:
            os.close(root)


def resume_media_deletions(workspace: Workspace) -> list[dict]:
    """Reconcile up to ten existing deletion records without choosing any new target."""
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        if not _table(database, "media_deletion_operations"):
            return []
        operations = database.execute(
            """
            SELECT * FROM media_deletion_operations WHERE state != 'completed'
            ORDER BY CASE state WHEN 'prepared' THEN 0 ELSE 1 END, id LIMIT 10
            """
        ).fetchall()
        results = []
        for operation in operations:
            root = _root_descriptor(workspace) if operation["root_inode"] else None
            try:
                results.append(_resume(database, workspace, root, operation))
            finally:
                if root is not None:
                    os.close(root)
        return results


def expire_session_comments(workspace: Workspace, session_id: int) -> dict:
    """Remove owned comment subtitles before removing their source comments."""
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        return _expire_comments(database, workspace, session_id)


def _expire_comments(database: sqlite3.Connection, workspace: Workspace, session_id: int) -> dict:
    previous = database.execute(
        "SELECT state, removed_comments FROM comment_expirations WHERE session_id = ?", (session_id,)
    ).fetchone()
    if previous and previous["state"] == "expired":
        return {"mode": workspace.mode, "state": "expired", "removed_comments": previous["removed_comments"]}
    deletion = _operation(database, session_id)
    if deletion is not None and deletion["state"] != "completed":
        raise MediaDeletionConflict("pending media deletion requires reconciliation")
    operation = _operation(database, session_id, comments=True)
    if operation is None:
        session = _assert_settled(database, session_id, comments=True)
        has_recording = database.execute(
            "SELECT 1 FROM recording_segments WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        needs_root = deletion is None and has_recording is not None
    else:
        needs_root = operation["root_inode"] != 0
    root = _root_descriptor(workspace) if needs_root else None
    try:
        if operation is None:
            database.executescript(_SCHEMA)
            operation = _prepare(database, workspace, root, session, "comments")
        report = _resume(database, workspace, root, operation)
        if report["state"] != "completed":
            raise MediaDeletionConflict("comment expiration requires review")
        count = database.execute(
            "SELECT removed_comments FROM comment_expirations WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        return {"mode": workspace.mode, "state": "expired", "removed_comments": count}
    finally:
        if root is not None:
            os.close(root)


def run_comment_retention(workspace: Workspace, keep_days: int, *, now: datetime | None = None) -> dict:
    """Expire at most one independently settled comment session per maintenance pass."""
    if type(keep_days) is not int or not 0 <= keep_days <= 36500:
        raise ValueError("invalid comment retention period")
    if keep_days == 0:
        return {"state": "disabled"}
    now = datetime.now(UTC) if now is None else now
    if now.utcoffset() is None:
        raise ValueError("retention clock must include its time zone")
    cutoff = (now.astimezone(UTC) - timedelta(days=keep_days)).isoformat()
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        if not _table(database, "session_settlements"):
            return {"state": "idle"}
        candidates = database.execute(
            """
            SELECT session.id FROM session_settlements AS settled
            JOIN stream_sessions AS session ON session.id = settled.session_id
            WHERE session.stream_state = 'ended' AND settled.ended_at <= ?
              AND NOT EXISTS (SELECT 1 FROM comment_expirations WHERE session_id = session.id)
            ORDER BY settled.ended_at, session.id LIMIT 20
            """,
            (cutoff,),
        ).fetchall()
        for candidate in candidates:
            session_id = candidate["id"]
            try:
                _assert_settled(database, session_id, comments=True)
            except MediaDeletionConflict:
                continue
            try:
                return _expire_comments(database, workspace, session_id)
            except (OSError, ValueError) as error:
                workspace.event("retention", "automatic_comment_expiration_blocked", str(session_id), error)
                return {"state": "blocked", "session_id": session_id}
        return {"state": "blocked" if candidates else "idle"}


def managed_recording_capacity(database: sqlite3.Connection) -> dict:
    """Summarize registered retained media; physical free space is a separate measurement."""
    rows = database.execute(
        """
        WITH files AS (
            SELECT session.streamer_id, segment.video_generation AS generation, 1 AS video
            FROM recording_segments AS segment
            JOIN stream_sessions AS session ON session.id = segment.session_id
            WHERE session.recording_state != 'deleted'
            UNION ALL
            SELECT session.streamer_id, subtitle.file_generation, 0 AS video
            FROM subtitle_publications AS subtitle
            JOIN recording_segments AS segment ON segment.id = subtitle.segment_id
            JOIN stream_sessions AS session ON session.id = segment.session_id
            WHERE session.recording_state != 'deleted'
        )
        SELECT streamer_id, sum(video), sum(json_extract(generation, '$[2]'))
        FROM files GROUP BY streamer_id ORDER BY streamer_id
        """
    ).fetchall()
    streamers = [
        {"streamer_id": streamer_id, "recording_files": files, "recorded_bytes": size}
        for streamer_id, files, size in rows
    ]
    return {
        "recording_files": sum(item["recording_files"] for item in streamers),
        "recorded_bytes": sum(item["recorded_bytes"] for item in streamers),
        "by_streamer": streamers,
    }


def _capacity_reason(database: sqlite3.Connection, root: int, policy: MediaRetentionPolicy) -> str | None:
    disk = os.fstatvfs(root)
    free = disk.f_bavail * disk.f_frsize
    if free < max(policy.minimum_free_bytes, 1):
        return "insufficient_space"
    if policy.maximum_bytes and managed_recording_capacity(database)["recorded_bytes"] >= policy.maximum_bytes:
        return "capacity_limit"
    return None


def run_media_retention(workspace: Workspace, policy: MediaRetentionPolicy, *, now: datetime | None = None) -> dict:
    """Process at most one oldest settled recording per maintenance pass."""
    if not policy.enabled:
        return {"state": "disabled"}
    if now is None:
        now = datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("retention clock must include its time zone")
    age_limit = (now.astimezone(UTC) - timedelta(days=policy.keep_days)).isoformat() if policy.keep_days else None
    workspace = workspace.recheck()
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        root = _root_descriptor(workspace)
        try:
            pressure = _capacity_reason(database, root, policy)
            if _table(database, "session_settlements"):
                candidates = database.execute(
                    """
                    SELECT session.*, settled.ended_at
                    FROM session_settlements AS settled
                    JOIN stream_sessions AS session ON session.id = settled.session_id
                    WHERE session.stream_state = 'ended'
                      AND session.recording_state IN ('completed', 'partial', 'cancelled')
                      AND EXISTS (SELECT 1 FROM recording_segments WHERE session_id = session.id)
                      AND (? OR settled.ended_at <= ?)
                    ORDER BY settled.ended_at, session.id LIMIT 20
                    """,
                    (pressure is not None, age_limit),
                ).fetchall()
            else:
                candidates = []
            result = _delete_oldest(database, workspace, root, candidates, pressure)
            if result["state"] != "idle":
                workspace.event("retention", f"automatic_{result['state']}", result["reason"])
            return result
        finally:
            os.close(root)


def _delete_oldest(
    database: sqlite3.Connection, workspace: Workspace, root: int, candidates: list[sqlite3.Row], pressure: str | None
) -> dict:
    reason = pressure or "retention_period"
    deferred = bool(candidates)
    for candidate in candidates:
        if _operation(database, candidate["id"]) is not None:
            continue
        try:
            session = _assert_settled(database, candidate["id"])
        except MediaDeletionConflict:
            continue
        database.executescript(_SCHEMA)
        try:
            operation = _prepare(database, workspace, root, session, "files")
        except (OSError, MediaDeletionConflict) as error:
            workspace.event("retention", "automatic_target_needs_review", str(session["id"]), error)
            return {"state": "blocked", "reason": "target_unverified", "session_id": session["id"]}
        report = _resume(database, workspace, root, operation)
        return {"state": report["state"], "reason": reason, "operation": report}
    if pressure or deferred:
        return {"state": "blocked", "reason": pressure or "no_settled_recording"}
    return {"state": "idle"}
