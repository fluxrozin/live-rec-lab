"""Associate observed TwitCasting comments with a verified isolated session."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

from ..budget import LivePermission
from ..db import connect
from ..isolation import FixtureWorkspace
from . import LiveObservation, LiveState
from .comment_pages import CommentBatch
from .monitor import ProbeClient

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comment_checkpoints (
    session_id INTEGER PRIMARY KEY REFERENCES stream_sessions(id) ON DELETE CASCADE,
    latest_id TEXT,
    state TEXT NOT NULL,
    reason TEXT,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comment_provider_attributes (
    comment_id INTEGER PRIMARY KEY REFERENCES comments(id) ON DELETE CASCADE,
    is_anonymous INTEGER NOT NULL CHECK (is_anonymous IN (0, 1)),
    anonymous_number INTEGER CHECK (anonymous_number > 0)
);
"""


@dataclass(frozen=True)
class TwitCastingPull:
    status: str
    latest_id: str | None
    saved: int
    reason: str | None


def _session(database: sqlite3.Connection, session_id: int, observation: LiveObservation) -> sqlite3.Row:
    row = database.execute(
        """
        SELECT stream_sessions.*, streamers.platform, streamers.normalized_id, streamers.enabled,
               streamers.archived_at, streamers.comments_enabled
        FROM stream_sessions JOIN streamers ON streamers.id = stream_sessions.streamer_id
        WHERE stream_sessions.id = ? AND NOT EXISTS (
            SELECT 1 FROM comment_expirations WHERE session_id = stream_sessions.id
        )
    """,
        (session_id,),
    ).fetchone()
    if not (
        row
        and observation.state is LiveState.LIVE
        and observation.service == "twitcasting"
        and row["platform"] == observation.service
        and row["normalized_id"] == observation.account
        and row["source_id"] == observation.live_id
    ):
        raise ValueError("TwitCasting observation does not match the session")
    return row


def sync_twitcasting_comments(
    workspace: FixtureWorkspace,
    session_id: int,
    client: ProbeClient,
    observation: LiveObservation,
    permission: LivePermission | None,
    client_id: str,
    client_secret: str,
) -> TwitCastingPull:
    with workspace.exclusive(worker="comments"):
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            session = _session(database, session_id, observation)
            if (
                session["stream_state"] != "live"
                or not session["enabled"]
                or not session["comments_enabled"]
                or session["archived_at"] is not None
            ):
                raise ValueError("TwitCasting comment collection is disabled for the session")
            database.executescript(_SCHEMA)
            checkpoint = database.execute(
                "SELECT latest_id FROM comment_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            previous = checkpoint["latest_id"] if checkpoint else None
        batch = client.twitcasting_comments(
            session["normalized_id"],
            observation,
            permission,
            client_id,
            client_secret,
            previous_id=previous,
        )
        observed_at = datetime.now(UTC).isoformat()
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            current = _session(database, session_id, observation)
            still_collecting = (
                current["stream_state"] == "live"
                and current["enabled"]
                and current["comments_enabled"]
                and current["archived_at"] is None
            )
            return _store(database, session_id, batch, previous, observed_at, still_collecting=still_collecting)


def _store(
    database: sqlite3.Connection,
    session_id: int,
    batch: CommentBatch,
    previous: str | None,
    observed_at: str,
    *,
    still_collecting: bool,
) -> TwitCastingPull:
    latest = batch.latest_id if batch.complete else previous
    saved = 0
    with database:
        for item in batch.comments:
            row = database.execute(
                """
                INSERT INTO comments (session_id, source_id, posted_at, received_at, offset_source,
                                      author_id, author_name, body)
                VALUES (?, ?, ?, ?, 'unverified', ?, ?, ?)
                ON CONFLICT(session_id, source_id) DO NOTHING RETURNING id
            """,
                (
                    session_id,
                    item.source_id,
                    item.posted_at.isoformat() if item.posted_at else None,
                    observed_at,
                    None if item.anonymous else item.author_id,
                    None if item.anonymous else item.author_name,
                    item.body,
                ),
            ).fetchone()
            if row:
                database.execute(
                    "INSERT INTO comment_provider_attributes VALUES (?, ?, ?)",
                    (row["id"], item.anonymous, item.anonymous_number if item.anonymous else None),
                )
                saved += 1
        has_observations = bool(
            database.execute(
                "SELECT 1 FROM comments WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        )
        status = "complete" if batch.complete else "partial" if has_observations else "unknown"
        database.execute(
            """
            INSERT INTO comment_checkpoints VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET latest_id=excluded.latest_id, state=excluded.state,
                reason=excluded.reason, observed_at=excluded.observed_at
        """,
            (session_id, latest, status, batch.reason, observed_at),
        )
        database.execute(
            "UPDATE stream_sessions SET comment_state = ? WHERE id = ?",
            (
                "running" if batch.complete and still_collecting else "partial" if has_observations else "unknown",
                session_id,
            ),
        )
    return TwitCastingPull(status, latest, saved, batch.reason)
