"""First collection path, driven by deterministic, offline fixture events."""

import os
import sqlite3
import stat
import subprocess
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import connect
from .isolation import FixtureWorkspace
from .media import automatic_stem, record_fixture_video
from .publication import matches_media, publish_initial_subtitle
from .subtitles import CommentCue, render_comment_srt

_MID_PIPELINE = ("pending", "running")


@dataclass(frozen=True)
class FixtureStream:
    platform: str
    source_id: str
    title: str
    streamer: str = "night-boat"
    started: datetime = datetime.fromisoformat("2026-11-24T21:18:00+09:00")


FIXTURE_STREAMS = {
    "tiktok": FixtureStream("tiktok", "synthetic-tiktok-001", "夜の選曲室"),
    "twitcasting": FixtureStream("twitcasting", "synthetic-twitcasting-001", "紙とインクの夜"),
}


def observe_fixture() -> bool | None:
    return True


def fixture_comments() -> Iterable[CommentCue]:
    return (
        CommentCue(450, "こんばんは 🌙", "夜のリスナー", "listener"),
        CommentCue(1150, "この曲好きです", "星空", "stargazer"),
    )


def run_fixture(workspace: FixtureWorkspace, service: str = "tiktok") -> dict[str, object]:
    stream = FIXTURE_STREAMS[service]
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        prior = find_fixture_session(database, stream)
        if prior is not None:
            _mark_interruption(database, prior, workspace, stream.source_id)
            return _report(database, prior["id"], workspace)
        observation = observe_fixture()
        state = "unknown" if observation is None else "live" if observation else "offline"
        workspace.event("monitor", state, stream.source_id)
        if state != "live":
            return {
                "mode": "fixture",
                "stream_state": state,
                "recording_state": "not_started",
                "comment_state": "not_started",
                "subtitle_state": "not_started",
                "comment_count": None,
                "video": None,
                "comment_srt": None,
            }

        with database:
            streamer = database.execute(
                "INSERT INTO streamers (platform, normalized_id, enabled) VALUES (?, ?, 1)",
                (stream.platform, stream.streamer),
            ).lastrowid
            session_id = database.execute(
                """
                INSERT INTO stream_sessions (streamer_id, source_id, title, started_at, stream_state)
                VALUES (?, ?, ?, ?, 'live')
                """,
                (streamer, stream.source_id, stream.title, stream.started.astimezone(UTC).isoformat()),
            ).lastrowid
        assert session_id is not None
        _record_comments(database, session_id, workspace, stream)
        _record_video(database, session_id, workspace, stream)
        _publish_comment_subtitle(database, session_id, workspace, stream.source_id)
        with database:
            database.execute("UPDATE stream_sessions SET stream_state = 'ended' WHERE id = ?", (session_id,))
        workspace.event("monitor", "ended", stream.source_id)
        return _report(database, session_id, workspace)


def find_fixture_session(database: sqlite3.Connection, stream: FixtureStream) -> sqlite3.Row | None:
    return database.execute(
        """
        SELECT stream_sessions.* FROM stream_sessions
        JOIN streamers ON streamers.id = streamer_id
        WHERE platform = ? AND normalized_id = ? AND source_id = ?
        """,
        (stream.platform, stream.streamer, stream.source_id),
    ).fetchone()


def _record_comments(
    database: sqlite3.Connection, session_id: int, workspace: FixtureWorkspace, stream: FixtureStream
) -> None:
    status = "completed"
    try:
        for number, cue in enumerate(fixture_comments(), start=1):
            posted_at = (stream.started + timedelta(milliseconds=cue.offset_ms)).astimezone(UTC).isoformat()
            with database:
                database.execute(
                    """
                    INSERT INTO comments (
                        session_id, source_id, posted_at, received_at, recording_offset_ms,
                        offset_source, author_id, author_name, body
                    ) VALUES (?, ?, ?, ?, ?, 'fixture_timeline', ?, ?, ?)
                    """,
                    (
                        session_id,
                        f"fixture-{number}",
                        posted_at,
                        posted_at,
                        cue.offset_ms,
                        cue.author_id,
                        cue.author_name,
                        cue.body,
                    ),
                )
    except (OSError, ValueError, sqlite3.Error) as error:
        count = database.execute("SELECT COUNT(*) FROM comments WHERE session_id = ?", (session_id,)).fetchone()[0]
        status = "partial" if count else "failed"
        workspace.event("comments", status, stream.source_id, error)
    with database:
        database.execute("UPDATE stream_sessions SET comment_state = ? WHERE id = ?", (status, session_id))


def _media_directory(database: sqlite3.Connection, workspace: FixtureWorkspace, path: Path) -> Path:
    relative_path = str(path.relative_to(workspace.root))
    existing = database.execute(
        "SELECT device, inode FROM media_directories WHERE relative_path = ?", (relative_path,)
    ).fetchone()
    if existing is None:
        path.mkdir(mode=0o700)
    current = path.lstat()
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or current.st_mode & 0o077
        or current.st_dev != workspace.recordings.stat().st_dev
    ):
        raise FileExistsError("fixture media directory is not safe")
    identity = current.st_dev, current.st_ino
    if existing is None:
        with database:
            database.execute("INSERT INTO media_directories VALUES (?, ?, ?)", (relative_path, *identity))
    elif tuple(existing) != identity:
        raise FileExistsError("fixture media directory identity changed")
    return path


def _record_video(
    database: sqlite3.Connection, session_id: int, workspace: FixtureWorkspace, stream: FixtureStream
) -> None:
    with database:
        database.execute("UPDATE stream_sessions SET recording_state = 'running' WHERE id = ?", (session_id,))
    try:
        streamer = _media_directory(database, workspace, workspace.recordings / stream.streamer)
        directory = _media_directory(database, workspace, streamer / stream.started.date().isoformat())
        stem = automatic_stem(stream.platform, stream.streamer, stream.started, stream.title, 1)
        video = directory / f"{stem}.mp4"
        duration_ms, video_generation = record_fixture_video(video)
        with database:
            database.execute(
                """
                INSERT INTO recording_segments (session_id, number, video_path, video_generation, duration_ms)
                VALUES (?, 1, ?, ?, ?)
                """,
                (session_id, str(video.relative_to(workspace.root)), video_generation, duration_ms),
            )
            database.execute("UPDATE stream_sessions SET recording_state = 'completed' WHERE id = ?", (session_id,))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        status = "needs_review" if isinstance(error, (FileExistsError, sqlite3.Error)) else "failed"
        with database:
            database.execute("UPDATE stream_sessions SET recording_state = ? WHERE id = ?", (status, session_id))
        workspace.event("recording", status, stream.source_id, error)


def comment_cues(database: sqlite3.Connection, session_id: int) -> list[CommentCue]:
    rows = database.execute(
        """
        SELECT recording_offset_ms, body, author_name, author_id FROM comments
        WHERE session_id = ? AND recording_offset_ms IS NOT NULL
        ORDER BY recording_offset_ms, id
        """,
        (session_id,),
    )
    return [CommentCue(*row) for row in rows]


def _publish_comment_subtitle(
    database: sqlite3.Connection, session_id: int, workspace: FixtureWorkspace, source_id: str
) -> None:
    segment = database.execute("SELECT * FROM recording_segments WHERE session_id = ?", (session_id,)).fetchone()
    state = database.execute("SELECT comment_state FROM stream_sessions WHERE id = ?", (session_id,)).fetchone()[0]
    status = "blocked"
    if segment and state in {"completed", "partial"}:
        cues = comment_cues(database, session_id)
        status = "blocked" if state == "partial" else "skipped"
        subtitles = render_comment_srt(cues, segment["duration_ms"])
        if subtitles:
            try:
                publish_initial_subtitle(database, workspace, segment, "comment", subtitles, len(cues))
                status = "completed" if state == "completed" else "partial"
            except (OSError, ValueError, sqlite3.Error) as error:
                status = "needs_review" if isinstance(error, (FileExistsError, sqlite3.Error)) else "failed"
                workspace.event("subtitle", status, source_id, error)
    with database:
        database.execute("UPDATE stream_sessions SET subtitle_state = ? WHERE id = ?", (status, session_id))


def _mark_interruption(
    database: sqlite3.Connection, prior: sqlite3.Row, workspace: FixtureWorkspace, source_id: str
) -> None:
    if prior["stream_state"] != "live":
        return
    with database:
        for stale in ("stream", "recording", "comment", "subtitle"):
            key = f"{stale}_state"
            if stale == "stream" or prior[key] in _MID_PIPELINE:
                database.execute(f"UPDATE stream_sessions SET {key} = 'interrupted' WHERE id = ?", (prior["id"],))
    workspace.event("monitor", "interrupted", source_id)


def _report(database: sqlite3.Connection, session_id: int, workspace: FixtureWorkspace) -> dict[str, object]:
    state = database.execute("SELECT * FROM stream_sessions WHERE id = ?", (session_id,)).fetchone()
    segment = database.execute("SELECT * FROM recording_segments WHERE session_id = ?", (session_id,)).fetchone()
    subtitle = database.execute(
        """
        SELECT subtitle_publications.* FROM subtitle_publications
        JOIN recording_segments ON recording_segments.id = segment_id
        WHERE session_id = ? AND kind = 'comment'
        """,
        (session_id,),
    ).fetchone()
    recording_state, subtitle_state = state["recording_state"], state["subtitle_state"]
    video_ok = bool(segment and matches_media(workspace, segment["video_path"], segment["video_generation"]))
    subtitle_ok = bool(
        video_ok and subtitle and matches_media(workspace, subtitle["relative_path"], subtitle["file_generation"])
    )
    if (segment or recording_state == "completed") and not video_ok:
        recording_state = "needs_review"
    if (subtitle or subtitle_state in {"completed", "partial"}) and not subtitle_ok:
        subtitle_state = "needs_review"
    if (recording_state, subtitle_state) != (state["recording_state"], state["subtitle_state"]):
        with database:
            database.execute(
                "UPDATE stream_sessions SET recording_state = ?, subtitle_state = ? WHERE id = ?",
                (recording_state, subtitle_state, session_id),
            )
        workspace.event("storage", "publication_requires_review", state["source_id"])
    count = database.execute("SELECT COUNT(*) FROM comments WHERE session_id = ?", (session_id,)).fetchone()[0]
    return {
        "mode": "fixture",
        "stream_state": state["stream_state"],
        "recording_state": recording_state,
        "comment_state": state["comment_state"],
        "subtitle_state": subtitle_state,
        "comment_count": count if count or state["comment_state"] == "completed" else None,
        "video": segment["video_path"] if video_ok else None,
        "comment_srt": subtitle["relative_path"] if subtitle_ok else None,
    }
