"""Owned, same-volume publication for fixture subtitles."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .isolation import FixtureWorkspace
from .media import generation, publish_exclusive

_SUFFIXES = {"comment": "_comment.srt", "speech": "_speech.srt", "ass": ".ass"}


def matches_media(workspace: FixtureWorkspace, relative_path: str, expected: str) -> bool:
    candidate = workspace.root / relative_path
    if ".." in candidate.parts or not candidate.is_relative_to(workspace.recordings):
        return False
    try:
        return candidate.resolve(strict=True) == candidate and generation(candidate) == expected
    except (OSError, ValueError):
        return False


def publish_initial_subtitle(
    database: sqlite3.Connection,
    workspace: FixtureWorkspace,
    segment: sqlite3.Row,
    kind: str,
    text: str,
    version: int,
) -> Path:
    if not matches_media(workspace, segment["video_path"], segment["video_generation"]):
        raise ValueError("video generation changed before subtitle staging")
    video = workspace.root / segment["video_path"]
    destination = video.with_name(video.stem + _SUFFIXES[kind])
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=video.parent,
        suffix=".part" + destination.suffix,
        delete=False,
    ) as staging:
        staging.write(text)
        staging.flush()
        os.fsync(staging.fileno())
        temporary = Path(staging.name)
    if not matches_media(workspace, segment["video_path"], segment["video_generation"]):
        raise ValueError("video generation changed before subtitle publication")
    publish_exclusive(temporary, destination)
    relative = destination.relative_to(workspace.root)
    with database:
        database.execute(
            """
            INSERT INTO subtitle_publications
            (segment_id, kind, relative_path, file_generation, input_version)
            VALUES (?, ?, ?, ?, ?)
            """,
            (segment["id"], kind, str(relative), generation(destination), version),
        )
    return relative


_REVISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS subtitle_revision_operations (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES recording_segments(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    destination TEXT NOT NULL,
    input_version INTEGER NOT NULL,
    prior_generation TEXT,
    new_identity TEXT,
    staged_path TEXT,
    staged_generation TEXT,
    backup_path TEXT,
    backup_generation TEXT,
    state TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS subtitle_revision_prepared
    ON subtitle_revision_operations(segment_id, kind) WHERE state = 'prepared';
"""


def _write_staged(directory: Path, contents: bytes, label: str) -> Path:
    with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{label}-", delete=False) as staging:
        staging.write(contents)
        staging.flush()
        os.fsync(staging.fileno())
        return Path(staging.name)


def _identity(file_generation: str) -> str:
    # Rename updates ctime even when it publishes precisely the staged inode.
    return json.dumps(json.loads(file_generation)[:4])


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tracked_file(path: Path, op: sqlite3.Row) -> str | None:
    try:
        version = generation(path)
    except FileNotFoundError:
        return None
    if _identity(version) != op["new_identity"]:
        raise ValueError("pending subtitle does not match its prepared generation")
    return version


def _commit_revision(database: sqlite3.Connection, op: sqlite3.Row, new_generation: str | None) -> None:
    with database:
        if new_generation:
            database.execute(
                """
                INSERT INTO subtitle_publications (segment_id, kind, relative_path, file_generation, input_version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(segment_id, kind) DO UPDATE SET relative_path=excluded.relative_path,
                    file_generation=excluded.file_generation, input_version=excluded.input_version
            """,
                (op["segment_id"], op["kind"], op["destination"], new_generation, op["input_version"]),
            )
        else:
            database.execute(
                "DELETE FROM subtitle_publications WHERE segment_id = ? AND kind = ?",
                (op["segment_id"], op["kind"]),
            )
        database.execute("UPDATE subtitle_revision_operations SET state = 'completed' WHERE id = ?", (op["id"],))


def _cleanup_operation(workspace: FixtureWorkspace, op: sqlite3.Row) -> None:
    for role in ("staged", "backup"):
        path, expected = op[f"{role}_path"], op[f"{role}_generation"]
        if not path or not expected or not matches_media(workspace, path, expected):
            continue
        try:
            (workspace.root / path).unlink()
        except OSError as error:
            workspace.event("subtitle", "temporary_cleanup_failed", f"subtitle-revision-{op['id']}", error)


def _reconcile(
    database: sqlite3.Connection,
    workspace: FixtureWorkspace,
    segment_id: int,
    kind: str,
    relative: str,
) -> None:
    op = database.execute(
        """
        SELECT * FROM subtitle_revision_operations WHERE segment_id = ? AND kind = ? AND state = 'prepared'
    """,
        (segment_id, kind),
    ).fetchone()
    if op is None:
        return
    if op["destination"] != relative:
        raise ValueError("pending subtitle location changed")
    path = workspace.root / relative
    if (not op["prior_generation"] and not os.path.lexists(path) and op["new_identity"]) or (
        op["prior_generation"] and matches_media(workspace, relative, op["prior_generation"])
    ):
        with database:
            database.execute("UPDATE subtitle_revision_operations SET state = 'aborted' WHERE id = ?", (op["id"],))
    else:
        generation_now = _tracked_file(path, op)
        if generation_now is None and op["new_identity"] is not None:
            raise ValueError("prepared publication could not be reconciled")
        _sync_directory(path.parent)
        _commit_revision(database, op, generation_now)
    _cleanup_operation(workspace, op)


def publish_subtitle_revision(
    database: sqlite3.Connection,
    workspace: FixtureWorkspace,
    segment: sqlite3.Row,
    kind: str,
    text: str,
    version: int,
) -> None:
    """Publish a revision under the fixture writer lock and journal the file mutation."""
    if not matches_media(workspace, segment["video_path"], segment["video_generation"]):
        raise ValueError("video generation changed before subtitle revision")
    video = workspace.root / segment["video_path"]
    destination = video.with_name(video.stem + _SUFFIXES[kind])
    relative = str(destination.relative_to(workspace.root))
    database.executescript(_REVISION_SCHEMA)
    _reconcile(database, workspace, segment["id"], kind, relative)
    prior = database.execute(
        """
        SELECT relative_path, file_generation FROM subtitle_publications WHERE segment_id = ? AND kind = ?
    """,
        (segment["id"], kind),
    ).fetchone()
    prior_generation = prior["file_generation"] if prior else None
    if prior:
        if prior["relative_path"] != relative or not matches_media(workspace, relative, prior_generation):
            raise ValueError("prior subtitle generation changed")
    elif os.path.lexists(destination):
        raise FileExistsError("subtitle path is occupied")
    elif not text:
        return
    staged = _write_staged(destination.parent, text.encode(), "subtitle-stage") if text else None
    staged_generation = generation(staged) if staged else None
    backup = _write_staged(destination.parent, destination.read_bytes(), "subtitle-previous") if prior else None
    with database:
        op = database.execute(
            """
            INSERT INTO subtitle_revision_operations (
                segment_id, kind, destination, input_version, prior_generation, new_identity,
                staged_path, staged_generation, backup_path, backup_generation, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared') RETURNING *
        """,
            (
                segment["id"],
                kind,
                relative,
                version,
                prior_generation,
                _identity(staged_generation) if staged_generation else None,
                str(staged.relative_to(workspace.root)) if staged else None,
                staged_generation,
                str(backup.relative_to(workspace.root)) if backup else None,
                generation(backup) if backup else None,
            ),
        ).fetchone()
    if not matches_media(workspace, segment["video_path"], segment["video_generation"]):
        raise ValueError("video generation changed before subtitle publication")
    if staged and not matches_media(workspace, op["staged_path"], staged_generation):
        raise ValueError("staged subtitle generation changed")
    if prior and not matches_media(workspace, relative, prior_generation):
        raise ValueError("prior subtitle generation changed")
    if staged and prior:
        os.replace(staged, destination)
    elif staged:
        publish_exclusive(staged, destination)
    else:
        destination.unlink()
    _sync_directory(destination.parent)
    published_generation = _tracked_file(destination, op)
    if staged and published_generation is None:
        raise ValueError("published subtitle disappeared")
    _commit_revision(database, op, published_generation)
    _cleanup_operation(workspace, op)


def has_prepared_ass(database: sqlite3.Connection, session_id: int) -> bool:
    if not database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subtitle_revision_operations'"
    ).fetchone():
        return False
    return (
        database.execute(
            """
        SELECT 1 FROM subtitle_revision_operations
        JOIN recording_segments ON recording_segments.id = subtitle_revision_operations.segment_id
        WHERE kind = 'ass' AND state = 'prepared' AND recording_segments.session_id = ?
    """,
            (session_id,),
        ).fetchone()
        is not None
    )
