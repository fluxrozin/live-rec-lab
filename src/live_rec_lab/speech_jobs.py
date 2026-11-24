"""The isolated speech queue and the parent-side publication boundary."""

import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .collection import comment_cues
from .db import connect
from .isolation import FixtureWorkspace
from .media import fixture_dimensions
from .publication import matches_media, publish_subtitle_revision
from .speech import SpeechBlocked, SpeechCue, render_combined_ass, render_speech_srt
from .speech_worker import AudioUnavailable, run_local_worker

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS speech_attempts (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES recording_segments(id) ON DELETE CASCADE,
    video_generation TEXT NOT NULL,
    state TEXT NOT NULL,
    model TEXT NOT NULL,
    language TEXT NOT NULL,
    device TEXT NOT NULL,
    origin TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS speech_attempts_active
    ON speech_attempts(segment_id) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS speech_attempts_latest ON speech_attempts(segment_id, id DESC);
CREATE TABLE IF NOT EXISTS speech_configuration (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    model TEXT NOT NULL,
    language TEXT NOT NULL,
    device TEXT NOT NULL,
    revision INTEGER NOT NULL
);
"""
_SEGMENT = """
SELECT recording_segments.*, stream_sessions.stream_state, stream_sessions.recording_state,
       stream_sessions.comment_state, stream_sessions.source_id
FROM recording_segments JOIN stream_sessions ON stream_sessions.id = recording_segments.session_id
WHERE recording_segments.id = ?
"""
_SETTLED_RECORDING = {"completed", "partial", "cancelled"}


@dataclass(frozen=True)
class SpeechConfiguration:
    enabled: bool = True
    model: str = "large-v3"
    language: str = "ja"
    device: str = "cuda"
    revision: int = 0

    def __post_init__(self) -> None:
        if self.model not in {"large-v3", "large-v3-turbo"}:
            raise ValueError("unsupported speech model")
        if self.device not in {"cuda", "cpu"}:
            raise ValueError("unsupported speech device")
        if self.language not in _LANGUAGES:
            raise ValueError("unsupported speech language")


# Whisper's language tokens, including Cantonese in the large-v3 vocabulary.
_LANGUAGES = frozenset(
    "en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta no th ur hr bg lt la "
    "mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be "
    "tg sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba jw su yue".split()
)


@dataclass(frozen=True)
class SpeechAttempt:
    id: int
    segment_id: int
    video_generation: str
    state: str
    model: str
    language: str
    device: str
    origin: str
    queued_at: str
    started_at: str | None
    finished_at: str | None
    reason: str | None


class SpeechSettingsConflict(ValueError):
    """Another request has saved a newer settings revision."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_speech_schema(database: sqlite3.Connection) -> None:
    database.executescript(_QUEUE_SCHEMA)


def _has_table(database: sqlite3.Connection, table: str) -> bool:
    return (
        database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        is not None
    )


def _attempt(row: sqlite3.Row) -> SpeechAttempt:
    return SpeechAttempt(**dict(row))


def read_speech_configuration(database: sqlite3.Connection) -> SpeechConfiguration:
    if not _has_table(database, "speech_configuration"):
        return SpeechConfiguration()
    row = database.execute("SELECT enabled, model, language, device, revision FROM speech_configuration").fetchone()
    if row is None:
        return SpeechConfiguration()
    values = dict(row)
    values["enabled"] = bool(values["enabled"])
    return SpeechConfiguration(**values)


def save_speech_configuration(workspace: FixtureWorkspace, configuration: SpeechConfiguration) -> SpeechConfiguration:
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        ensure_speech_schema(database)
        with database:
            database.execute("BEGIN IMMEDIATE")
            current = read_speech_configuration(database)
            if current.revision != configuration.revision:
                raise SpeechSettingsConflict("speech settings have changed")
            database.execute(
                """
                INSERT INTO speech_configuration VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, model=excluded.model,
                    language=excluded.language, device=excluded.device, revision=excluded.revision
                """,
                (
                    configuration.enabled,
                    configuration.model,
                    configuration.language,
                    configuration.device,
                    current.revision + 1,
                ),
            )
        return read_speech_configuration(database)


def latest_speech(database: sqlite3.Connection, segment_id: int) -> SpeechAttempt | None:
    if not _has_table(database, "speech_attempts"):
        return None
    row = database.execute(
        "SELECT * FROM speech_attempts WHERE segment_id = ? ORDER BY id DESC LIMIT 1", (segment_id,)
    ).fetchone()
    return _attempt(row) if row else None


def speech_attempts(workspace: FixtureWorkspace) -> list[SpeechAttempt]:
    workspace = FixtureWorkspace.open(workspace.root)
    workspace.require_ready()
    with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        if not _has_table(database, "speech_attempts"):
            return []
        return [_attempt(row) for row in database.execute("SELECT * FROM speech_attempts ORDER BY id DESC LIMIT 200")]


def _verified_segment(
    database: sqlite3.Connection,
    workspace: FixtureWorkspace,
    segment_id: int,
    input_generation: str | None = None,
) -> sqlite3.Row:
    segment = database.execute(_SEGMENT, (segment_id,)).fetchone()
    if segment is None:
        raise ValueError("recorded segment not found")
    if segment["stream_state"] != "ended" or segment["recording_state"] not in _SETTLED_RECORDING:
        raise ValueError("speech requires a settled recording")
    if (input_generation is not None and segment["video_generation"] != input_generation) or not matches_media(
        workspace,
        segment["video_path"],
        segment["video_generation"],
    ):
        raise ValueError("video generation changed")
    return segment


def _queue(
    database: sqlite3.Connection,
    segment: sqlite3.Row,
    configuration: SpeechConfiguration,
    origin: str,
) -> SpeechAttempt:
    row = database.execute(
        """
        INSERT INTO speech_attempts (segment_id, video_generation, state, model, language, device, origin, queued_at)
        VALUES (?, ?, 'queued', ?, ?, ?, ?, ?) RETURNING *
    """,
        (
            segment["id"],
            segment["video_generation"],
            configuration.model,
            configuration.language,
            configuration.device,
            origin,
            _now(),
        ),
    ).fetchone()
    return _attempt(row)


def enqueue_speech(workspace: FixtureWorkspace, segment_id: int) -> SpeechAttempt:
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        ensure_speech_schema(database)
        segment = _verified_segment(database, workspace, segment_id)
        with database:
            previous = latest_speech(database, segment_id)
            configuration = read_speech_configuration(database)
            if previous and previous.state == "queued" and previous.origin == "automatic":
                row = database.execute(
                    """
                    UPDATE speech_attempts SET origin = 'manual', model = ?, language = ?, device = ?
                    WHERE id = ? RETURNING *
                """,
                    (configuration.model, configuration.language, configuration.device, previous.id),
                ).fetchone()
                return _attempt(row)
            if previous and previous.state in {"queued", "running"}:
                return previous
            return _queue(database, segment, configuration, "manual")


def _enqueue_ready(database: sqlite3.Connection, workspace: FixtureWorkspace) -> None:
    configuration = read_speech_configuration(database)
    if not configuration.enabled:
        return
    candidates = database.execute("""
        SELECT recording_segments.id FROM recording_segments
        JOIN stream_sessions ON stream_sessions.id = recording_segments.session_id
        WHERE stream_state = 'ended' AND recording_state IN ('completed', 'partial', 'cancelled')
        AND NOT EXISTS (SELECT 1 FROM speech_attempts WHERE segment_id = recording_segments.id)
        AND NOT EXISTS (
            SELECT 1 FROM subtitle_publications WHERE segment_id = recording_segments.id AND kind = 'speech'
        )
    """).fetchall()
    with database:
        for row in candidates:
            try:
                segment = _verified_segment(database, workspace, row["id"])
            except ValueError:
                continue
            _queue(database, segment, configuration, "automatic")


def _finish(database: sqlite3.Connection, attempt_id: int, state: str, reason: str | None = None) -> SpeechAttempt:
    with database:
        row = database.execute(
            """
            UPDATE speech_attempts SET state = ?, reason = ?, finished_at = ? WHERE id = ? RETURNING *
        """,
            (state, reason, _now(), attempt_id),
        ).fetchone()
    return _attempt(row)


def _claim(database: sqlite3.Connection, workspace: FixtureWorkspace, automatic: bool) -> SpeechAttempt | None:
    ensure_speech_schema(database)
    with database:
        database.execute(
            """
            UPDATE speech_attempts SET state = 'interrupted', reason = 'worker_stopped', finished_at = ?
            WHERE state = 'running'
        """,
            (_now(),),
        )
    abandoned = database.execute("""
        SELECT prior.* FROM speech_attempts prior WHERE prior.state = 'interrupted'
        AND NOT EXISTS (SELECT 1 FROM speech_attempts newer WHERE newer.segment_id = prior.segment_id
                        AND newer.id > prior.id)
    """).fetchall()
    for row in abandoned:
        try:
            segment = _verified_segment(database, workspace, row["segment_id"], row["video_generation"])
        except ValueError:
            continue
        with database:
            settings = SpeechConfiguration(model=row["model"], language=row["language"], device=row["device"])
            _queue(database, segment, settings, row["origin"])
    if automatic:
        _enqueue_ready(database, workspace)
    configuration = read_speech_configuration(database)
    with database:
        row = database.execute(
            """
            UPDATE speech_attempts SET state = 'running', started_at = ?, model = ?, language = ?, device = ?
            WHERE id = (
                SELECT id FROM speech_attempts WHERE state = 'queued' AND (origin = 'manual' OR ?)
                ORDER BY (origin = 'manual') DESC, id LIMIT 1
            ) RETURNING *
        """,
            (_now(), configuration.model, configuration.language, configuration.device, configuration.enabled),
        ).fetchone()
    return _attempt(row) if row else None


def process_speech(
    workspace: FixtureWorkspace,
    recognize: Callable[[FixtureWorkspace, SpeechAttempt, Path], list[SpeechCue]] | None = None,
    *,
    automatic: bool = False,
) -> SpeechAttempt | None:
    with workspace.exclusive(worker="speech") as worker_lock:
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            attempt = _claim(database, workspace, automatic)
            if not attempt:
                return None
            try:
                segment = _verified_segment(database, workspace, attempt.segment_id, attempt.video_generation)
            except ValueError:
                return _finish(database, attempt.id, "needs_review", "input_generation_changed")
        try:
            video = workspace.root / segment["video_path"]
            speech = (
                run_local_worker(
                    workspace,
                    video,
                    model_name=attempt.model,
                    language=attempt.language,
                    device=attempt.device,
                    worker_lock=worker_lock,
                )
                if recognize is None
                else recognize(workspace, attempt, video)
            )
        except AudioUnavailable:
            return _end_attempt(workspace, attempt, "not_applicable", "no_audio")
        except SpeechBlocked as error:
            return _end_attempt(workspace, attempt, "blocked", getattr(error, "reason", "inference_unavailable"))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return _end_attempt(workspace, attempt, "failed", "inference_error")
        except KeyboardInterrupt:
            _end_attempt(workspace, attempt, "interrupted", "worker_stopped")
            raise
        return _publish(workspace, attempt, speech)


def _end_attempt(workspace: FixtureWorkspace, attempt: SpeechAttempt, state: str, reason: str) -> SpeechAttempt:
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        result = _finish(database, attempt.id, state, reason)
    workspace.event("speech", reason, f"speech-{attempt.id}")
    return result


def _publish(workspace: FixtureWorkspace, attempt: SpeechAttempt, cues: list[SpeechCue]) -> SpeechAttempt:
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        try:
            segment = _verified_segment(database, workspace, attempt.segment_id, attempt.video_generation)
        except ValueError:
            return _finish(database, attempt.id, "needs_review", "input_generation_changed")
        speech = render_speech_srt(cues, segment["duration_ms"])
        try:
            publish_subtitle_revision(database, workspace, segment, "speech", speech, attempt.id + 1)
        except (OSError, ValueError, sqlite3.Error):
            return _finish(database, attempt.id, "needs_review", "speech_publication_failed")
        ass_reason = _initial_ass(database, workspace, attempt, segment, cues)
        return _finish(database, attempt.id, "completed" if speech else "no_speech", ass_reason)


def _initial_ass(
    database: sqlite3.Connection,
    workspace: FixtureWorkspace,
    attempt: SpeechAttempt,
    segment: sqlite3.Row,
    cues: list[SpeechCue],
) -> str | None:
    if database.execute("SELECT 1 FROM comment_expirations WHERE session_id = ?", (segment["session_id"],)).fetchone():
        return "ass_retention_blocked"
    comments = comment_cues(database, segment["session_id"])
    try:
        if not (comments or render_speech_srt(cues, segment["duration_ms"])):
            publish_subtitle_revision(database, workspace, segment, "ass", "", attempt.id + 1)
            return None
        width, height = fixture_dimensions(workspace.root / segment["video_path"])
        ass = render_combined_ass(cues, comments, segment["duration_ms"], width, height)
        publish_subtitle_revision(
            database,
            workspace,
            segment,
            "ass",
            ass.text if ass.dialogue_count else "",
            attempt.id + 1,
        )
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return "ass_publication_failed"
    return "ass_partial" if segment["comment_state"] != "completed" or ass.omitted_comments else None


def retry_failed_speech(workspace: FixtureWorkspace) -> tuple[list[SpeechAttempt], list[int]]:
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        ensure_speech_schema(database)
        candidates = database.execute("""
            SELECT prior.segment_id FROM speech_attempts prior
            WHERE prior.state = 'failed' AND NOT EXISTS (
                SELECT 1 FROM speech_attempts newer WHERE newer.segment_id = prior.segment_id AND newer.id > prior.id
            )
            ORDER BY prior.id
        """).fetchall()
        configuration = read_speech_configuration(database)
        accepted = []
        skipped = []
        with database:
            database.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                segment_id = candidate["segment_id"]
                try:
                    segment = _verified_segment(database, workspace, segment_id)
                except ValueError:
                    skipped.append(segment_id)
                    continue
                accepted.append(_queue(database, segment, configuration, "manual"))
    return accepted, skipped


def public_attempt(attempt: SpeechAttempt) -> dict[str, object]:
    return {
        "id": attempt.id,
        "segment_id": attempt.segment_id,
        "state": attempt.state,
        "reason": attempt.reason,
        "model": attempt.model,
        "language": attempt.language,
        "device": attempt.device,
        "origin": attempt.origin,
        "queued_at": attempt.queued_at,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


def published_speech_uses_job(database: sqlite3.Connection, segment_id: int, input_version: int) -> bool:
    if not _has_table(database, "speech_attempts"):
        return False
    return (
        database.execute(
            "SELECT 1 FROM speech_attempts WHERE id = ? AND segment_id = ?",
            (input_version - 1, segment_id),
        ).fetchone()
        is not None
    )
