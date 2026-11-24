"""Durable ASS generation from confirmed per-segment subtitle input revisions."""

import json
import os
import re
import sqlite3
import stat
import subprocess
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Event

from .db import connect
from .isolation import Workspace
from .media import fixture_dimensions
from .publication import matches_media, publish_subtitle_revision
from .speech import AssResult, SpeechCue, render_combined_ass
from .speech_inputs import SEGMENT, verified_segment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ass_attempts (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES recording_segments(id) ON DELETE CASCADE,
    video_generation TEXT NOT NULL,
    state TEXT NOT NULL,
    input_token TEXT,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    reason TEXT,
    dialogue_count INTEGER,
    omitted_comments INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ass_attempts_active
    ON ass_attempts(segment_id) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ass_attempts_latest ON ass_attempts(segment_id, id DESC);
"""
_TIME = r"([0-9]{2,}:[0-9]{2}:[0-9]{2},[0-9]{3})"
_CUE_TIME = re.compile(rf"{_TIME} --> {_TIME}")
_BODY_LIMIT = 8_000_000


@dataclass(frozen=True)
class AssAttempt:
    id: int
    segment_id: int
    video_generation: str
    state: str
    input_token: str | None
    queued_at: str
    started_at: str | None
    finished_at: str | None
    reason: str | None
    dialogue_count: int | None
    omitted_comments: int | None


@dataclass(frozen=True)
class _Source:
    relative_path: str
    file_generation: str
    input_version: int


@dataclass(frozen=True)
class _Inputs:
    video_generation: str
    duration_ms: int
    comment_state: str
    comment_subtitle_state: str
    speech_state: str
    speech: _Source | None
    comment: _Source | None
    presentation_version: int = 1

    def token(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


class _AssExpired(ValueError):
    """Recording is subject to comment retention."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _has_table(database: sqlite3.Connection, table: str) -> bool:
    return (
        database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    )


def _attempt(row: sqlite3.Row | None) -> AssAttempt | None:
    return AssAttempt(**dict(row)) if row is not None else None


def latest_ass(database: sqlite3.Connection, segment_id: int) -> AssAttempt | None:
    if not _has_table(database, "ass_attempts"):
        return None
    return _attempt(
        database.execute(
            "SELECT * FROM ass_attempts WHERE segment_id = ? ORDER BY id DESC LIMIT 1", (segment_id,)
        ).fetchone()
    )


def public_ass_attempt(attempt: AssAttempt) -> dict[str, object]:
    result = asdict(attempt)
    result.pop("video_generation")
    result.pop("input_token")
    return result


def _expired(database: sqlite3.Connection, segment: sqlite3.Row) -> bool:
    return (
        segment["comment_state"] == "expired"
        or database.execute(
            "SELECT 1 FROM comment_expirations WHERE session_id = ?", (segment["session_id"],)
        ).fetchone()
        is not None
    )


def _insert(database: sqlite3.Connection, segment: sqlite3.Row) -> AssAttempt:
    row = database.execute(
        """
        INSERT INTO ass_attempts (segment_id, video_generation, state, queued_at)
        VALUES (?, ?, 'queued', ?) RETURNING *
        """,
        (segment["id"], segment["video_generation"], _now()),
    ).fetchone()
    return AssAttempt(**dict(row))


def queue_ass_after_speech(database: sqlite3.Connection, segment: sqlite3.Row) -> None:
    """Enqueue ASS under the existing writer lock after a speech result changes."""
    if _expired(database, segment):
        return
    database.executescript(_SCHEMA)
    with database:
        if not _active_ass(database, segment["id"]):
            _insert(database, segment)


def _active_ass(database: sqlite3.Connection, segment_id: int) -> AssAttempt | None:
    return _attempt(
        database.execute(
            "SELECT * FROM ass_attempts WHERE segment_id = ? AND state IN ('queued', 'running')",
            (segment_id,),
        ).fetchone()
    )


def enqueue_ass(workspace: Workspace, segment_id: int) -> AssAttempt:
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        segment = verified_segment(database, workspace, segment_id)
        if _expired(database, segment):
            raise _AssExpired("ASS generation is blocked for expired or expiring comments")
        database.executescript(_SCHEMA)
        with database:
            return _active_ass(database, segment_id) or _insert(database, segment)


def _inputs(database: sqlite3.Connection, segment: sqlite3.Row) -> _Inputs:
    publications = {
        row["kind"]: _Source(row["relative_path"], row["file_generation"], row["input_version"])
        for row in database.execute(
            """
            SELECT kind, relative_path, file_generation, input_version FROM subtitle_publications
            WHERE segment_id = ? AND kind IN ('speech', 'comment')
            """,
            (segment["id"],),
        )
    }
    speech = publications.get("speech")
    latest = None
    if _has_table(database, "speech_attempts"):
        latest = database.execute(
            "SELECT state FROM speech_attempts WHERE segment_id = ? ORDER BY id DESC LIMIT 1",
            (segment["id"],),
        ).fetchone()
    speech_state = latest["state"] if latest is not None else ("completed" if speech else "pending")
    if speech_state in {"queued", "running"}:
        speech_state = "pending"
    return _Inputs(
        segment["video_generation"],
        segment["duration_ms"],
        segment["comment_state"],
        segment["subtitle_state"],
        speech_state,
        speech,
        publications.get("comment"),
    )


def _queue_changed(database: sqlite3.Connection, workspace: Workspace) -> None:
    # Inspect lightweight SQLite revisions first. Only reserve one candidate on an idle tick.
    for row in database.execute(
        SEGMENT
        + """
        WHERE stream_sessions.stream_state = 'ended'
        AND stream_sessions.recording_state IN ('completed', 'partial', 'cancelled')
        AND stream_sessions.comment_state != 'expired'
        AND NOT EXISTS (SELECT 1 FROM comment_expirations WHERE session_id = stream_sessions.id)
        ORDER BY recording_segments.id
        """
    ):
        prior = latest_ass(database, row["id"])
        if prior and prior.input_token == _inputs(database, row).token() and prior.state != "interrupted":
            continue
        try:
            segment = verified_segment(database, workspace, row["id"])
        except ValueError:
            continue
        _insert(database, segment)
        return


def _claim(database: sqlite3.Connection, workspace: Workspace, automatic: bool) -> AssAttempt | None:
    database.executescript(_SCHEMA)
    with database:
        database.execute(
            """
            UPDATE ass_attempts SET state = 'interrupted', reason = 'worker_stopped', finished_at = ?
            WHERE state = 'running'
            """,
            (_now(),),
        )
        if automatic and not database.execute("SELECT 1 FROM ass_attempts WHERE state = 'queued' LIMIT 1").fetchone():
            _queue_changed(database, workspace)
        row = database.execute(
            """
            UPDATE ass_attempts SET state = 'running', started_at = ?
            WHERE id = (SELECT id FROM ass_attempts WHERE state = 'queued' ORDER BY id LIMIT 1)
            RETURNING *
            """,
            (_now(),),
        ).fetchone()
    return _attempt(row)


def _finish(
    database: sqlite3.Connection,
    attempt: AssAttempt,
    state: str,
    reason: str | None = None,
    *,
    inputs: _Inputs | None = None,
    dialogue_count: int | None = None,
    omitted_comments: int | None = None,
) -> AssAttempt:
    with database:
        row = database.execute(
            """
            UPDATE ass_attempts
            SET state = ?, reason = ?, input_token = ?, finished_at = ?, dialogue_count = ?, omitted_comments = ?
            WHERE id = ? RETURNING *
            """,
            (state, reason, inputs.token() if inputs else None, _now(), dialogue_count, omitted_comments, attempt.id),
        ).fetchone()
    return AssAttempt(**dict(row))


def _timestamp(value: str) -> int:
    clock, milliseconds = value.split(",")
    hours, minutes, seconds = map(int, clock.split(":"))
    if minutes > 59 or seconds > 59:
        raise ValueError("invalid subtitle timestamp")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(milliseconds)


def _verify_source(workspace: Workspace, source: _Source | None) -> None:
    if source is not None and not matches_media(workspace, source.relative_path, source.file_generation):
        raise ValueError("subtitle source generation changed")


def _read_cues(workspace: Workspace, source: _Source | None) -> list[SpeechCue]:
    if source is None:
        return []
    _verify_source(workspace, source)
    path = workspace.media_path(source.relative_path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as subtitle:
        details = os.fstat(subtitle.fileno())
        identity = [details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns]
        if not stat.S_ISREG(details.st_mode) or json.dumps(identity) != source.file_generation:
            raise ValueError("subtitle source generation changed while opening")
        content = subtitle.read(_BODY_LIMIT + 1)
    if len(content) > _BODY_LIMIT:
        raise ValueError("subtitle source is too large for ASS generation")
    _verify_source(workspace, source)
    cues = []
    for block in content.strip().split("\n\n"):
        if not block:
            continue
        number, _, caption = block.partition("\n")
        times, _, body = caption.partition("\n")
        match = _CUE_TIME.fullmatch(times)
        if not number.isascii() or not number.isdecimal() or match is None or not body.strip():
            raise ValueError("managed subtitle contains an invalid cue")
        start, end = _timestamp(match[1]), _timestamp(match[2])
        if start >= end:
            raise ValueError("managed subtitle contains an inverted cue")
        cues.append(SpeechCue(start, end, body))
    return cues


def _verify_ass(database: sqlite3.Connection, workspace: Workspace, attempt: AssAttempt) -> tuple[sqlite3.Row, _Inputs]:
    segment = verified_segment(database, workspace, attempt.segment_id, attempt.video_generation)
    if _expired(database, segment):
        raise _AssExpired("comments expired before ASS publication")
    return segment, _inputs(database, segment)


def _outcome(inputs: _Inputs, omitted: int) -> tuple[str, str | None]:
    if omitted:
        return "partial", "comments_omitted"
    if inputs.comment_state not in {"completed", "disabled"} or inputs.comment_subtitle_state in {
        "partial",
        "needs_review",
        "failed",
        "blocked",
    }:
        return "partial", "comments_incomplete"
    if inputs.speech_state == "pending":
        return "provisional", "speech_pending"
    if inputs.speech_state not in {"completed", "no_speech", "not_applicable"}:
        return "partial", "speech_previous_result" if inputs.speech else "speech_unavailable"
    return "completed", None


def process_ass(
    workspace: Workspace,
    *,
    automatic: bool = False,
    stop: Event | None = None,
) -> AssAttempt | None:
    """Run one ASS attempt with media rendering outside the writer lock."""
    if stop is not None and stop.is_set():
        return None
    workspace.require_media()
    with workspace.exclusive(worker="ass"):
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            attempt = _claim(database, workspace, automatic)
            if attempt is None:
                return None
            try:
                segment, inputs = _verify_ass(database, workspace, attempt)
            except _AssExpired:
                return _finish(database, attempt, "expired", "comments_expired")
            except ValueError:
                return _finish(database, attempt, "needs_review", "input_generation_changed")

        result = None
        error = None
        try:
            speech = _read_cues(workspace, inputs.speech)
            comments = _read_cues(workspace, inputs.comment)
            if speech or comments:
                width, height = fixture_dimensions(workspace.media_path(segment["video_path"]), timeout=5)
                result = render_combined_ass(speech, comments, inputs.duration_ms, width, height)
        except (OSError, ValueError, subprocess.SubprocessError) as caught:
            error = caught

        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            return _publish(database, workspace, attempt, inputs, result, error, stop)


def _publish(
    database: sqlite3.Connection,
    workspace: Workspace,
    attempt: AssAttempt,
    inputs: _Inputs,
    result: AssResult | None,
    error: Exception | None,
    stop: Event | None,
) -> AssAttempt:
    def verify_sources() -> None:
        _verify_source(workspace, inputs.speech)
        _verify_source(workspace, inputs.comment)

    if stop is not None and stop.is_set():
        return _finish(database, attempt, "interrupted", "worker_stopped", inputs=inputs)
    try:
        segment, current = _verify_ass(database, workspace, attempt)
        if current != inputs:
            return _finish(database, attempt, "interrupted", "input_revision_changed", inputs=inputs)
        verify_sources()
    except _AssExpired:
        return _finish(database, attempt, "expired", "comments_expired", inputs=inputs)
    except ValueError:
        return _finish(database, attempt, "needs_review", "input_generation_changed", inputs=inputs)
    if error is not None:
        workspace.event("ass", "generation_failed", f"ass-{attempt.id}", error)
        return _finish(database, attempt, "failed", "generation_failed", inputs=inputs)

    dialogue_count = result.dialogue_count if result else 0
    omitted = result.omitted_comments if result else 0
    state, reason = _outcome(inputs, omitted)
    try:
        if dialogue_count or state == "completed":
            publish_subtitle_revision(
                database,
                workspace,
                segment,
                "ass",
                result.text if result and dialogue_count else "",
                attempt.id,
                verify_inputs=verify_sources,
            )
    except (OSError, ValueError, sqlite3.Error) as error:
        workspace.event("ass", "publication_failed", f"ass-{attempt.id}", error)
        return _finish(database, attempt, "needs_review", "publication_failed", inputs=inputs)
    if state == "completed" and not dialogue_count:
        state = "skipped"
    elif not dialogue_count:
        state = "blocked"
    return _finish(
        database, attempt, state, reason, inputs=inputs, dialogue_count=dialogue_count, omitted_comments=omitted
    )
