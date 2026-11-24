"""Scoped fixture retention using an operation marker and media generations."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .collection import FIXTURE_STREAMS, find_fixture_session
from .db import connect
from .isolation import FixtureWorkspace, IsolationError, Workspace
from .media import generation
from .publication import matches_media
from .retention import expire_session_comments


def expire_fixture_comments(workspace: FixtureWorkspace, platform: str) -> dict[str, object]:
    if workspace.mode != "fixture":
        raise IsolationError("fixture expiration requires a fixture workspace")
    stream = FIXTURE_STREAMS[platform]
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        session = find_fixture_session(database, stream)
        if session is None:
            raise ValueError("expiration requires an ended fixture session")
    return expire_session_comments(workspace, session["id"])


_BACKUP_NAME = re.compile(r"fixture-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.sqlite3\Z")
_RESTORE_TABLES = {
    "streamers",
    "stream_sessions",
    "recording_segments",
    "comments",
    "subtitle_publications",
    "comment_expirations",
}
_RESTORE_PROTECTED_TABLES = (
    "streamers",
    "media_directories",
    "speech_attempts",
    "comment_checkpoints",
    "comment_provider_attributes",
    "subtitle_revision_operations",
    "media_deletion_operations",
    "media_deletion_targets",
    "session_settlements",
    "provider_monitor_state",
    "provider_media_folders",
    "provider_recording_attempts",
    "provider_recording_segments",
    "provider_recording_failures",
    "provider_recording_names",
    "ass_attempts",
)
_RESTORE_COUNTS = {
    "conflicting_session_identity": """
        SELECT count(*) FROM candidate.stream_sessions former
        JOIN candidate.streamers former_owner ON former_owner.id = former.streamer_id
        JOIN main.stream_sessions current ON current.id = former.id
        JOIN main.streamers current_owner ON current_owner.id = current.streamer_id
        WHERE former.source_id != current.source_id OR former_owner.platform != current_owner.platform
            OR former_owner.normalized_id != current_owner.normalized_id
    """,
    "expired_comments_in_backup": """
        SELECT count(*) FROM candidate.comments former
        JOIN main.comment_expirations expiry ON expiry.session_id = former.session_id
    """,
    "expired_comment_subtitles_in_backup": """
        SELECT count(*) FROM candidate.subtitle_publications former
        JOIN candidate.recording_segments segment ON segment.id = former.segment_id
        JOIN main.comment_expirations expiry ON expiry.session_id = segment.session_id
        WHERE former.kind IN ('comment', 'ass')
    """,
    "unrecorded_expirations_in_backup": """
        SELECT count(*) FROM main.comment_expirations current
        WHERE NOT EXISTS (
            SELECT 1 FROM candidate.comment_expirations former WHERE former.session_id = current.session_id
                AND former.state = current.state AND former.completed_at IS current.completed_at
        )
    """,
    "newer_recordings_missing_from_backup": """
        SELECT count(*) FROM main.recording_segments current
        WHERE NOT EXISTS (
            SELECT 1 FROM candidate.recording_segments former WHERE former.id = current.id
                AND former.session_id = current.session_id AND former.video_generation = current.video_generation
        )
    """,
    "newer_subtitles_missing_from_backup": """
        SELECT count(*) FROM main.subtitle_publications current
        WHERE NOT EXISTS (
            SELECT 1 FROM candidate.subtitle_publications former WHERE former.segment_id = current.segment_id
                AND former.kind = current.kind AND former.file_generation = current.file_generation
        )
    """,
    "newer_comments_missing_from_backup": """
        SELECT count(*) FROM main.comments current
        WHERE NOT EXISTS (
            SELECT 1 FROM candidate.comments former WHERE former.id = current.id
                AND former.session_id = current.session_id AND former.source_id = current.source_id
                AND former.body = current.body
        )
    """,
}


def inspect_fixture_restore(workspace: Workspace, snapshot_name: str) -> dict[str, object]:
    """Check a backup against the isolated workspace with two read-only schemas."""
    workspace = workspace.recheck()
    workspace.require_ready()
    candidate = fixture_backup_path(workspace, snapshot_name)
    issues = check_fixture_restore(workspace, workspace.database, candidate)
    return {
        "mode": "fixture",
        "snapshot": snapshot_name,
        "state": "blocked" if issues else "reviewable",
        "issues": issues,
    }


def fixture_backup_path(workspace: Workspace, snapshot_name: str) -> Path:
    directory = workspace.events.parent / "backups"
    if not _BACKUP_NAME.fullmatch(snapshot_name):
        raise ValueError("invalid fixture backup name")
    candidate = directory / snapshot_name
    try:
        workspace.check_directory(directory, private=True)
        generation(candidate)
    except (OSError, ValueError) as error:
        raise ValueError("fixture backup is not a verified owned file") from error
    return candidate


def check_fixture_restore(
    workspace: Workspace, current: Path, candidate: Path, *, immutable_current: bool = False
) -> dict[str, int]:
    original_generation = generation(candidate)
    uri = current.as_uri() + ("?mode=ro&immutable=1" if immutable_current else "?mode=ro")

    issues = {}
    with closing(sqlite3.connect(uri, uri=True)) as database:
        database.execute("PRAGMA query_only = ON")
        database.execute("PRAGMA trusted_schema = OFF")
        database.execute("ATTACH DATABASE ? AS candidate", (candidate.as_uri() + "?mode=ro&immutable=1",))
        with database:
            database.execute("BEGIN")
            tables = {row[0] for row in database.execute("SELECT name FROM candidate.sqlite_master WHERE type='table'")}
            current_version = database.execute("PRAGMA main.user_version").fetchone()[0]
            candidate_version = database.execute("PRAGMA candidate.user_version").fetchone()[0]
            if current_version != candidate_version or not _RESTORE_TABLES <= tables:
                issues["incompatible_schema"] = 1
            elif database.execute("PRAGMA candidate.integrity_check").fetchall() != [("ok",)] or (
                database.execute("PRAGMA candidate.foreign_key_check").fetchone() is not None
            ):
                issues["failed_integrity_check"] = 1
            else:
                issues.update(_changed_protected_tables(database, tables))
                deleted = _deleted_media_in_candidate(database, tables)
                for issue, statement in _RESTORE_COUNTS.items():
                    count = database.execute(statement).fetchone()[0]
                    if count:
                        issues[issue] = count
                for kind, statement in (
                    (
                        "unverified_backup_videos",
                        "SELECT video_path, video_generation FROM candidate.recording_segments",
                    ),
                    (
                        "unverified_backup_subtitles",
                        "SELECT relative_path, file_generation FROM candidate.subtitle_publications",
                    ),
                ):
                    total = sum(
                        (path, file_generation) not in deleted and not matches_media(workspace, path, file_generation)
                        for path, file_generation in database.execute(statement)
                    )
                    if total:
                        issues[kind] = total
    try:
        unchanged = generation(candidate) == original_generation
    except (OSError, ValueError):
        unchanged = False
    if not unchanged:
        issues["backup_changed_during_check"] = 1
    return issues


def _deleted_media_in_candidate(database: sqlite3.Connection, tables: set[str]) -> set[tuple[str, str]]:
    if not {"media_deletion_operations", "media_deletion_targets"} <= tables:
        return set()
    return set(
        database.execute(
            """
        SELECT target.relative_path, target.file_generation
        FROM candidate.media_deletion_targets AS target
        JOIN candidate.media_deletion_operations AS operation ON operation.id = target.operation_id
        WHERE target.state = 'removed' AND operation.state = 'completed'
        """
        )
    )


def _changed_protected_tables(database: sqlite3.Connection, candidate_tables: set[str]) -> dict[str, int]:
    current_tables = {row[0] for row in database.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
    issues = {}
    for table in _RESTORE_PROTECTED_TABLES:
        if table not in current_tables | candidate_tables:
            continue
        if table not in current_tables & candidate_tables:
            issues[f"changed_{table}"] = 1
            continue
        current_columns = [column[1] for column in database.execute(f"PRAGMA main.table_info({table})")]
        candidate_columns = [column[1] for column in database.execute(f"PRAGMA candidate.table_info({table})")]
        if current_columns != candidate_columns or any(
            database.execute(f"SELECT * FROM {left}.{table} EXCEPT SELECT * FROM {right}.{table} LIMIT 1").fetchone()
            is not None
            for left, right in (("main", "candidate"), ("candidate", "main"))
        ):
            issues[f"changed_{table}"] = 1
    return issues
