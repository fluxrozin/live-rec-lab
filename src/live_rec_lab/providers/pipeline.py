"""Run one monitored account through independent, scoped acquisition paths."""

import sqlite3
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from ..budget import LivePermission
from ..db import connect
from ..publication import matches_media, publish_initial_subtitle
from ..retention import deleted_source, next_session_id
from ..subtitles import CommentCue, render_comment_srt
from . import LiveObservation, LiveState
from .collection import _SCHEMA as COMMENT_SCHEMA
from .collection import _store
from .comment_pages import CommentBatch
from .monitor import ProbeClient
from .recordings import RecordingInput, recording_schema, save_recording
from .tiktok_events import TikTokCommentBatch

if TYPE_CHECKING:
    from ..isolation import Workspace

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_monitor_state (
    streamer_id INTEGER PRIMARY KEY REFERENCES streamers(id),
    status TEXT NOT NULL,
    reason TEXT,
    observed_at TEXT NOT NULL,
    last_live_at TEXT,
    offline_since TEXT
);
"""
_END_GRACE = timedelta(seconds=15)
_ORPHAN_GRACE = timedelta(seconds=120)
_ACQUISITION_ERRORS = (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError)


@dataclass(frozen=True)
class PollResult:
    state: LiveState
    session_id: int | None = None
    stream_state: str | None = None
    reason: str | None = None


class CollectionRunner:
    def __init__(
        self,
        workspace: "Workspace",
        probe: ProbeClient,
        *,
        record: Callable[[LiveObservation], RecordingInput | None] | None = None,
        tiktok_comments: Callable[[LiveObservation, str | None], TikTokCommentBatch] | None = None,
        twitcasting_credentials: tuple[str, str] = ("", ""),
        twitcasting_monitor_limit: int = 10,
        ending_grace: timedelta = _END_GRACE,
        orphan_grace: timedelta = _ORPHAN_GRACE,
        recording_retries: int = 2,
    ) -> None:
        self.workspace = workspace
        self.probe = probe
        self.record = record
        self.tiktok_comments = tiktok_comments
        self.twitcasting_credentials = twitcasting_credentials
        self.twitcasting_monitor_limit = twitcasting_monitor_limit
        self.ending_grace = ending_grace
        self.orphan_grace = orphan_grace
        if type(recording_retries) is not int or recording_retries < 0:
            raise ValueError("recording retry limit must be a nonnegative integer")
        self.recording_retries = recording_retries

    def poll(self, streamer_id: int, permission: LivePermission | None, *, now: datetime | None = None) -> PollResult:
        observed_at = now or datetime.now(UTC)
        if observed_at.utcoffset() is None:
            raise ValueError("observations require an explicit timezone")
        observed_at = observed_at.astimezone(UTC)
        if self.probe.stopping:
            return PollResult(LiveState.UNKNOWN, reason="stopping")
        with self.workspace.exclusive(worker="collection"):
            with self.workspace.exclusive(), closing(connect(self.workspace.database)) as database:
                account = self._registered(database, streamer_id)
                if account is None:
                    return PollResult(LiveState.UNKNOWN, reason="monitoring_disabled")
                active = self._active(database, streamer_id)
            platform, target = account["platform"], account["normalized_id"]
            if platform == "tiktok":
                observation = self.probe.tiktok(target, permission)
            else:
                observation = self.probe.twitcasting(
                    target,
                    permission,
                    *self.twitcasting_credentials,
                    monitor_limit=self.twitcasting_monitor_limit,
                    known_movie_id=active["source_id"] if active is not None else None,
                )
            with self.workspace.exclusive(), closing(connect(self.workspace.database)) as database:
                account = self._registered(database, streamer_id)
                if account is None:
                    return PollResult(LiveState.UNKNOWN, reason="monitoring_disabled")
                database.executescript(_SCHEMA)
                database.executescript(COMMENT_SCHEMA)
                recording_schema(database)
                outcome = self._observe(database, account, observation, observed_at)
                if outcome.stream_state == "ended" and outcome.session_id is not None:
                    self._comment_subtitles(database, outcome.session_id)
                if outcome.state is not LiveState.LIVE or outcome.stream_state != "live":
                    return outcome
                session = database.execute(
                    """
                    SELECT stream_sessions.*, streamers.platform, streamers.normalized_id
                    FROM stream_sessions JOIN streamers ON streamers.id = streamer_id
                    WHERE stream_sessions.id = ?
                    """,
                    (outcome.session_id,),
                ).fetchone()
                checkpoint = database.execute(
                    "SELECT latest_id FROM comment_checkpoints WHERE session_id = ?", (outcome.session_id,)
                ).fetchone()
                previous = checkpoint["latest_id"] if checkpoint else None
                allow_recording = self._recording_ready(database, session, account, permission)
                allow_comments = account["comments_enabled"] and self._permitted(account, permission, "comments")
                if not allow_comments and session["comment_state"] == "pending":
                    with database:
                        database.execute(
                            "UPDATE stream_sessions SET comment_state = ? WHERE id = ?",
                            ("blocked" if account["comments_enabled"] else "not_started", session["id"]),
                        )
            self._collect(session, observation, permission, previous, allow_recording, allow_comments)
            return outcome

    @staticmethod
    def _registered(database: sqlite3.Connection, streamer_id: int) -> sqlite3.Row | None:
        return database.execute(
            "SELECT * FROM streamers WHERE id = ? AND enabled = 1 AND archived_at IS NULL", (streamer_id,)
        ).fetchone()

    @staticmethod
    def _active(database: sqlite3.Connection, streamer_id: int) -> sqlite3.Row | None:
        return database.execute(
            """
            SELECT * FROM stream_sessions WHERE streamer_id = ? AND stream_state IN ('live', 'ending', 'restricted')
            ORDER BY id DESC LIMIT 1
            """,
            (streamer_id,),
        ).fetchone()

    def _observe(
        self, database: sqlite3.Connection, account: sqlite3.Row, observation: LiveObservation, observed_at: datetime
    ) -> PollResult:
        account_id = account["id"]
        active = self._active(database, account_id)
        prior = database.execute("SELECT * FROM provider_monitor_state WHERE streamer_id = ?", (account_id,)).fetchone()
        last_live = prior["last_live_at"] if prior else None
        pending_since = prior["offline_since"] if prior else None
        outcome = PollResult(observation.state, reason=observation.reason)
        with database:
            match = observation.service == account["platform"] and observation.account == account["normalized_id"]
            if observation.state is LiveState.LIVE and observation.live_id and match:
                last_live, pending_since = observed_at.isoformat(), None
                outcome = self._live(database, account_id, observation, observed_at, active)
            elif active is not None:
                stream_state, pending_since = self._inactive(database, active, observation, observed_at, prior, match)
                outcome = PollResult(observation.state, active["id"], stream_state, observation.reason)
            database.execute(
                """
                INSERT INTO provider_monitor_state VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(streamer_id) DO UPDATE SET status=excluded.status, reason=excluded.reason,
                    observed_at=excluded.observed_at, last_live_at=excluded.last_live_at,
                    offline_since=excluded.offline_since
                """,
                (account_id, outcome.state, outcome.reason, observed_at.isoformat(), last_live, pending_since),
            )
        if prior is None or prior["status"] != outcome.state or prior["reason"] != outcome.reason:
            self.workspace.event("monitor", outcome.state, uuid4().hex)
        return outcome

    @staticmethod
    def _live(
        database: sqlite3.Connection,
        streamer_id: int,
        observation: LiveObservation,
        observed_at: datetime,
        active: sqlite3.Row | None,
    ) -> PollResult:
        source_id = observation.live_id
        same = database.execute(
            "SELECT id, stream_state FROM stream_sessions WHERE streamer_id = ? AND source_id = ?",
            (streamer_id, source_id),
        ).fetchone()
        if deleted_source(database, streamer_id, source_id):
            return PollResult(LiveState.LIVE, same["id"] if same else None, reason="managed_files_deleted")
        if same and same["stream_state"] == "ended":
            return PollResult(LiveState.LIVE, same["id"], "ended", "already_ended")
        if active is not None and active["source_id"] != source_id:
            database.execute("UPDATE stream_sessions SET stream_state = 'interrupted' WHERE id = ?", (active["id"],))
        if same is None:
            session_id = next_session_id(database)
            database.execute(
                "INSERT INTO stream_sessions (id, streamer_id, source_id, title, started_at, stream_state) "
                "VALUES (?, ?, ?, ?, ?, 'live')",
                (
                    session_id,
                    streamer_id,
                    source_id,
                    (observation.title or "untitled")[:500],
                    (observation.started_at or observed_at).astimezone(UTC).isoformat(),
                ),
            )
        else:
            session_id = same["id"]
            database.execute("UPDATE stream_sessions SET stream_state = 'live' WHERE id = ?", (session_id,))
        return PollResult(LiveState.LIVE, session_id, "live")

    def _inactive(
        self,
        database: sqlite3.Connection,
        active: sqlite3.Row,
        observation: LiveObservation,
        observed_at: datetime,
        prior: sqlite3.Row | None,
        match: bool,
    ) -> tuple[str, str | None]:
        session_id = active["id"]
        state = active["stream_state"]
        pending = prior["offline_since"] if prior else None
        same_source = observation.live_id in (None, active["source_id"])
        if observation.state is LiveState.RESTRICTED and match and same_source:
            database.execute(
                """
                UPDATE stream_sessions SET stream_state = 'restricted',
                    recording_state = CASE WHEN recording_state IN ('pending', 'running') THEN 'blocked'
                        ELSE recording_state END,
                    comment_state = CASE WHEN EXISTS (SELECT 1 FROM comments WHERE session_id = stream_sessions.id)
                        THEN 'partial' ELSE 'blocked' END
                WHERE id = ?
                """,
                (session_id,),
            )
            return "restricted", None
        if observation.state is LiveState.OFFLINE and match and same_source:
            if pending and observed_at - datetime.fromisoformat(pending) >= self.ending_grace:
                self._finalize(database, active, observed_at)
                return "ended", None
            database.execute("UPDATE stream_sessions SET stream_state = 'ending' WHERE id = ?", (session_id,))
            return "ending", pending or observed_at.isoformat()
        if (
            observation.state in {LiveState.UNKNOWN, LiveState.ERROR}
            and prior
            and prior["last_live_at"]
            and observed_at - datetime.fromisoformat(prior["last_live_at"]) >= self.orphan_grace
            and "running" not in (active["recording_state"], active["comment_state"])
        ):
            database.execute("UPDATE stream_sessions SET stream_state = 'interrupted' WHERE id = ?", (session_id,))
            return "interrupted", None
        return state, pending

    def _recording_ready(
        self,
        database: sqlite3.Connection,
        session: sqlite3.Row,
        account: sqlite3.Row,
        permission: LivePermission | None,
    ) -> bool:
        if deleted_source(database, account["id"], session["source_id"]):
            return False
        previous = database.execute(
            "SELECT state FROM provider_recording_attempts WHERE session_id = ?", (session["id"],)
        ).fetchall()
        if any(attempt["state"] in {"running", "needs_review"} for attempt in previous):
            with database:
                database.execute(
                    "UPDATE stream_sessions SET recording_state = 'needs_review' WHERE id = ?", (session["id"],)
                )
            return False
        if self.record and account["recording_enabled"] and self._permitted(account, permission, "record"):
            completed = database.execute(
                "SELECT 1 FROM provider_recording_segments WHERE session_id = ? AND completes_session",
                (session["id"],),
            ).fetchone()
            failed = database.execute(
                "SELECT attempts FROM provider_recording_failures WHERE session_id = ?", (session["id"],)
            ).fetchone()
            return not completed and (failed is None or failed["attempts"] <= self.recording_retries)
        if session["recording_state"] == "pending":
            with database:
                database.execute(
                    "UPDATE stream_sessions SET recording_state = ? WHERE id = ?",
                    ("blocked" if account["recording_enabled"] else "not_started", session["id"]),
                )
        return False

    @staticmethod
    def _permitted(account: sqlite3.Row, permission: LivePermission | None, operation: str) -> bool:
        return bool(
            permission
            and permission.allows(account["platform"], account["normalized_id"], operation, datetime.now(UTC))
        )

    def _collect(
        self,
        session: sqlite3.Row,
        observation: LiveObservation,
        permission: LivePermission | None,
        previous: str | None,
        allow_recording: bool,
        allow_comments: bool,
    ) -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            comments = executor.submit(self._comments, observation, permission, previous) if allow_comments else None
            try:
                if allow_recording and not self.probe.stopping:
                    self._capture(session, observation)
            finally:
                if comments:
                    self._save_comments(session, comments.result(), previous)

    def _capture(self, session: sqlite3.Row, observation: LiveObservation) -> None:
        try:
            self.workspace.require_media()
            source = self.record(observation)
            if source is None:
                return
            save_recording(self.workspace, session, source)
        except Exception as error:
            with self.workspace.exclusive(), closing(connect(self.workspace.database)) as database, database:
                recording_status = "needs_review" if isinstance(error, (sqlite3.Error, FileExistsError)) else "failed"
                database.execute(
                    """
                    INSERT INTO provider_recording_failures VALUES (?, 1)
                    ON CONFLICT(session_id) DO UPDATE SET attempts = attempts + 1
                    """,
                    (session["id"],),
                )
                database.execute(
                    """
                    UPDATE stream_sessions SET recording_state =
                        CASE WHEN ? = 'failed' AND EXISTS (
                            SELECT 1 FROM recording_segments WHERE session_id = stream_sessions.id
                        ) THEN 'partial' ELSE ? END
                    WHERE id = ?
                    """,
                    (recording_status, recording_status, session["id"]),
                )
            self.workspace.event("recording", "acquisition_failed", uuid4().hex, ValueError(type(error).__name__))

    def _comments(
        self, observation: LiveObservation, permission: LivePermission | None, previous: str | None
    ) -> CommentBatch:
        try:
            if self.probe.stopping:
                return CommentBatch((), previous, False, "stopping")
            if observation.service == "twitcasting":
                return self.probe.twitcasting_comments(
                    observation.account, observation, permission, *self.twitcasting_credentials, previous_id=previous
                )
            if self.tiktok_comments:
                delivered = self.tiktok_comments(observation, previous)
                if (delivered.account, delivered.room_id) != (observation.account, observation.live_id):
                    return CommentBatch((), previous, False, "room_mismatch")
                if delivered.dropped:
                    self.workspace.event(
                        "comments",
                        "unobserved_events",
                        uuid4().hex,
                        ValueError(f"discarded_events={delivered.dropped}"),
                    )
                return delivered.batch
            return CommentBatch((), previous, False, "comment_source_unavailable")
        except Exception as error:
            self.workspace.event("comments", "acquisition_failed", uuid4().hex, ValueError(type(error).__name__))
            return CommentBatch((), previous, False, "comment_collection_failed")

    def _save_comments(self, session: sqlite3.Row, batch: CommentBatch, previous: str | None) -> None:
        with (
            self.workspace.exclusive(worker="comments"),
            self.workspace.exclusive(),
            closing(connect(self.workspace.database)) as database,
        ):
            current = self._registered(database, session["streamer_id"])
            collecting = bool(current and current["comments_enabled"])
            _store(database, session["id"], batch, previous, datetime.now(UTC).isoformat(), still_collecting=collecting)

    def _finalize(self, database: sqlite3.Connection, active: sqlite3.Row, observed_at: datetime) -> None:
        from ..retention import record_session_end

        session_id = active["id"]
        status = active["recording_state"]
        comment_state = active["comment_state"]
        if (
            status == "partial"
            and database.execute(
                "SELECT 1 FROM provider_recording_segments WHERE session_id = ? AND completes_session", (session_id,)
            ).fetchone()
        ):
            status = "completed"
        if comment_state == "running":
            # The last successful poll does not prove that the terminal interval was observed.
            comment_state = "partial"
        database.execute(
            "UPDATE stream_sessions SET stream_state = 'ended', recording_state = ?, comment_state = ? WHERE id = ?",
            (status, comment_state, session_id),
        )
        record_session_end(database, session_id, observed_at)

    def _comment_subtitles(self, database: sqlite3.Connection, session_id: int) -> None:
        comment_state = database.execute(
            "SELECT comment_state FROM stream_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0]
        if database.execute("SELECT 1 FROM comment_expirations WHERE session_id = ?", (session_id,)).fetchone():
            return
        subtitle_state = "blocked"
        segments = database.execute("SELECT * FROM recording_segments WHERE session_id = ?", (session_id,)).fetchall()
        for segment in segments:
            cues = provider_comment_cues(database, segment)
            if not cues:
                continue
            text = render_comment_srt(cues, segment["duration_ms"])
            if not text:
                continue
            try:
                self.workspace.require_media()
                prior = database.execute(
                    "SELECT file_generation, relative_path FROM subtitle_publications "
                    "WHERE segment_id = ? AND kind = 'comment'",
                    (segment["id"],),
                ).fetchone()
                if prior:
                    if not matches_media(self.workspace, prior["relative_path"], prior["file_generation"]):
                        raise ValueError("comment subtitle generation changed")
                else:
                    publish_initial_subtitle(database, self.workspace, segment, "comment", text, len(cues))
                if subtitle_state != "needs_review":
                    subtitle_state = "completed" if comment_state == "completed" else "partial"
            except _ACQUISITION_ERRORS as error:
                subtitle_state = "needs_review"
                self.workspace.event("subtitle", subtitle_state, uuid4().hex, ValueError(type(error).__name__))
        with database:
            database.execute("UPDATE stream_sessions SET subtitle_state = ? WHERE id = ?", (subtitle_state, session_id))


def provider_comment_cues(database: sqlite3.Connection, segment: sqlite3.Row) -> list[CommentCue] | None:
    if (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'provider_recording_segments'"
        ).fetchone()
        is None
    ):
        return None
    timeline = database.execute(
        "SELECT started_at FROM provider_recording_segments WHERE segment_id = ?", (segment["id"],)
    ).fetchone()
    if timeline is None:
        return None
    start = datetime.fromisoformat(timeline["started_at"])
    end = start + timedelta(milliseconds=segment["duration_ms"])
    rows = database.execute(
        "SELECT posted_at, body, author_name, author_id FROM comments WHERE session_id = ? "
        "AND posted_at >= ? AND posted_at < ? ORDER BY posted_at, id",
        (segment["session_id"], start.isoformat(), end.isoformat()),
    )
    return [
        CommentCue(
            (datetime.fromisoformat(row["posted_at"]) - start) // timedelta(milliseconds=1),
            row["body"],
            row["author_name"],
            row["author_id"],
        )
        for row in rows
    ]
