"""配信者と配信記録の状態遷移。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import CollectionGap, Streamer, StreamSession
from live_rec_lab.monitoring.state import OPEN_SESSION
from live_rec_lab.platforms.base import Snapshot
from live_rec_lab.services.retention import deletion_pending
from live_rec_lab.storage import normalize_account


@dataclass(frozen=True)
class Intent:
    kind: str
    session_id: int
    platform: str = ""
    account: str = ""
    media_url: str | None = None
    manual: bool = False
    reason: str | None = None


def register_streamer(
    db: Session,
    *,
    platform: str,
    account: str,
    display_name: str,
    monitoring: bool,
    recording: bool,
    comments: bool,
) -> Streamer:
    if any(type(value) is not bool for value in (monitoring, recording, comments)):
        raise ValueError("監視・録画・コメント収集の全設定を指定してください")
    parsed = normalize_account(platform, account)
    existing = db.scalar(
        select(Streamer).where(
            Streamer.platform == platform,
            Streamer.platform_user_id == parsed.platform_user_id,
        )
    )
    if existing is not None:
        if existing.archived_at is None:
            raise ValueError("同じアカウントは登録済みです")
        existing.archived_at = None
        existing.live_status = "UNKNOWN"
        update_streamer(
            db, existing, display_name=display_name, monitoring=monitoring, recording=recording, comments=comments,
        )
        db.flush()
        return existing
    now = utcnow()
    streamer = Streamer(
        platform=platform,
        platform_user_id=parsed.platform_user_id,
        display_name=display_name.strip()[:128] or parsed.platform_user_id,
        profile_url=parsed.profile_url,
        monitoring_enabled=monitoring,
        recording_enabled=recording,
        comments_enabled=comments,
        live_status="UNKNOWN" if monitoring else "DISABLED",
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(streamer)
    except IntegrityError as exc:
        raise ValueError("同じアカウントは登録済みです") from exc
    return streamer


def update_streamer(
    db: Session,
    streamer: Streamer,
    *,
    display_name: str,
    monitoring: bool,
    recording: bool,
    comments: bool,
) -> None:
    streamer.display_name = display_name.strip()[:128] or streamer.platform_user_id
    streamer.monitoring_enabled = monitoring
    streamer.recording_enabled = recording
    streamer.comments_enabled = comments
    if not monitoring:
        streamer.live_status = "DISABLED"
    elif streamer.live_status == "DISABLED":
        streamer.live_status = "UNKNOWN"
    streamer.updated_at = utcnow()


def archive_streamer(db: Session, streamer: Streamer) -> bool:
    """配信履歴のある配信者は一意キーを維持してアーカイブする。"""
    history = db.scalar(select(StreamSession.id).where(StreamSession.streamer_id == streamer.id).limit(1))
    if history is None:
        db.delete(streamer)
        return False
    streamer.monitoring_enabled = False
    streamer.live_status = "DISABLED"
    streamer.archived_at = utcnow()
    streamer.updated_at = streamer.archived_at
    return True


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def open_sessions(db: Session, streamer_id: int) -> list[StreamSession]:
    return list(
        db.scalars(
            select(StreamSession).where(
                StreamSession.streamer_id == streamer_id,
                StreamSession.status.in_(OPEN_SESSION),
            )
        )
    )


def end_session(
    session: StreamSession, now: datetime, *, reason: str, platform_ended_at: datetime | None = None,
) -> list[Intent]:
    session.status = "ENDED" if reason == "platform_end" else "ENDED_UNCONFIRMED"
    session.end_reason = reason
    session.ended_at = session.updated_at = now
    session.platform_ended_at = platform_ended_at
    session.ending_since = None
    session.orphan_since = None
    return [
        Intent("stop_recording", session.id, reason=reason),
        Intent("stop_comments", session.id, reason=reason),
    ]


def job_intents(session: StreamSession, streamer: Streamer, snap: Snapshot) -> list[Intent]:
    recording = streamer.recording_enabled and not session.manual_stop and not snap.restricted
    comments = streamer.comments_enabled and session.comments_purged_at is None and not snap.restricted
    return [
        Intent(
            "start_recording" if recording else "stop_recording",
            session.id,
            platform=streamer.platform,
            account=streamer.platform_user_id,
            media_url=snap.media_url,
            reason=(
                "restricted_movie" if snap.restricted else "manual_stop" if session.manual_stop
                else "recording_disabled"
            ),
        ),
        Intent(
            "start_comments" if comments else "stop_comments",
            session.id,
            platform=streamer.platform,
            account=streamer.platform_user_id,
            reason="restricted_movie" if snap.restricted else "comments_disabled",
        ),
    ]


def _end_unconfirmed_sessions(
    db: Session,
    streamer: Streamer,
    now: datetime,
    *,
    recorder_alive: Callable[[int], bool],
    comments_alive: Callable[[int], bool],
    orphan_recheck: float,
) -> list[Intent]:
    intents: list[Intent] = []
    for session in open_sessions(db, streamer.id):
        if recorder_alive(session.id) or comments_alive(session.id):
            session.orphan_since = None
            continue
        if session.orphan_since is None:
            session.orphan_since = now
        if _as_utc(now) - _as_utc(session.orphan_since) < timedelta(seconds=orphan_recheck):
            continue
        for kind, enabled in (("recording", streamer.recording_enabled), ("comments", streamer.comments_enabled)):
            if enabled:
                db.add(CollectionGap(
                    stream_session_id=session.id,
                    job_type=kind,
                    started_at=session.last_seen_live_at or session.detected_at,
                    ended_at=now,
                    reason="platform_end_unconfirmed",
                    created_at=now,
                ))
        intents.extend(end_session(session, now, reason="platform_end_unconfirmed"))
    return intents


def apply_snapshot(
    db: Session,
    streamer: Streamer,
    snap: Snapshot,
    now: datetime,
    *,
    grace: float,
    recorder_alive: Callable[[int], bool],
    comments_alive: Callable[[int], bool] = lambda _session_id: False,
    orphan_recheck: float = 120,
) -> list[Intent]:
    intents: list[Intent] = []
    if not streamer.monitoring_enabled or streamer.archived_at is not None:
        streamer.live_status = "DISABLED"
        for session in open_sessions(db, streamer.id):
            intents.extend(end_session(session, now, reason="monitoring_disabled"))
        return intents
    streamer.last_checked_at = now
    streamer.updated_at = now
    streamer.live_status = "UNKNOWN" if snap.status == "LIVE" and not snap.platform_stream_id else snap.status
    if streamer.live_status in {"LIVE", "OFFLINE"}:
        streamer.last_ok_at = now

    if streamer.live_status in {"UNKNOWN", "ERROR"}:
        return _end_unconfirmed_sessions(
            db, streamer, now,
            recorder_alive=recorder_alive, comments_alive=comments_alive, orphan_recheck=orphan_recheck,
        )
    if snap.status == "OFFLINE":
        for session in open_sessions(db, streamer.id):
            session.orphan_since = None
            if session.ending_since is None:
                session.ending_since = now
                session.status = "ENDING"
                session.updated_at = now
            elif _as_utc(now) - _as_utc(session.ending_since) >= timedelta(seconds=grace):
                intents.extend(end_session(session, now, reason="platform_end", platform_ended_at=snap.ended_at))
        return intents
    if snap.status != "LIVE" or not snap.platform_stream_id:
        return intents

    streamer.last_live_at = now
    session = db.scalar(
        select(StreamSession).where(
            StreamSession.streamer_id == streamer.id,
            StreamSession.platform_stream_id == snap.platform_stream_id,
        )
    )
    if session is not None and deletion_pending(db, session.id):
        return intents
    if session is None:
        session = StreamSession(
            streamer_id=streamer.id,
            platform_stream_id=snap.platform_stream_id,
            title=snap.title,
            platform_started_at=snap.started_at,
            detected_at=now,
            manual_stop=False,
            created_at=now,
        )
        db.add(session)
    session.status = "LIVE"
    session.last_seen_live_at = session.updated_at = now
    session.ending_since = session.orphan_since = None
    session.ended_at = session.platform_ended_at = session.end_reason = None
    if snap.title:
        session.title = snap.title[:200]
    if snap.started_at and session.platform_started_at is None:
        session.platform_started_at = snap.started_at
    db.flush()
    for other in open_sessions(db, streamer.id):
        if other.id != session.id:
            intents.extend(end_session(other, now, reason="superseded_by_new_stream"))
    intents.extend(job_intents(session, streamer, snap))
    return intents
