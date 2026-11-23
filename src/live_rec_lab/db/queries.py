"""設定、検索、削除。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import Session

from live_rec_lab.config import (
    DEFAULTS,
    decode_setting,
    encode_setting,
    utcnow,
    validate_setting,
)
from live_rec_lab.db.models import (
    CollectionGap,
    Comment,
    CommentCollectionJob,
    RecordingJob,
    RecordingSegment,
    Streamer,
    StreamSession,
    SystemEvent,
    SystemSetting,
)
from live_rec_lab.ops.logging_config import redact

log = logging.getLogger("live_rec_lab.events")


def load_settings(db: Session) -> dict[str, object]:
    found = {row.key: row.value for row in db.scalars(select(SystemSetting))}
    values = dict(DEFAULTS)
    for key, raw in found.items():
        if key in DEFAULTS:
            values[key] = decode_setting(key, raw)
    env_root = os.environ.get("LIVE_REC_LAB_RECORDING_ROOT", "").strip()
    if env_root and "recording_root" not in found:
        values["recording_root"] = validate_setting("recording_root", env_root)
    return values


def save_settings(db: Session, updates: dict[str, object]) -> dict[str, object]:
    current = load_settings(db)
    now = utcnow()
    for key, value in updates.items():
        parsed = validate_setting(key, value)
        if key == "srt_display_seconds" and parsed != current[key]:
            db.execute(update(RecordingSegment).where(
                RecordingSegment.validation_status == "valid",
                RecordingSegment.files_deleted_at.is_(None),
                RecordingSegment.srt_status != "expired",
            ).values(srt_input_version=RecordingSegment.srt_input_version + 1, srt_status="stale"))
        current[key] = parsed
        row = db.get(SystemSetting, key)
        encoded = encode_setting(parsed)
        if row is None:
            db.add(SystemSetting(key=key, value=encoded, updated_at=now))
        else:
            row.value = encoded
            row.updated_at = now
    return current


def seed_settings(db: Session) -> None:
    found = set(db.scalars(select(SystemSetting.key)))
    save_settings(db, {key: value for key, value in DEFAULTS.items() if key not in found and key != "recording_root"})


def settings_revision(settings: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def add_event(
    db: Session,
    event_type: str,
    severity: str,
    message: str,
    stream_session_id: int | None = None,
    metadata: dict | None = None,
) -> None:
    detail = redact(message)
    safe = detail
    meta = None
    if metadata:
        from live_rec_lab.ops.logging_config import redact_fields

        meta = json.dumps(redact_fields(metadata), ensure_ascii=False)
    log.log(
        {"error": logging.ERROR, "warning": logging.WARNING}.get(severity, logging.INFO),
        detail,
        extra={"event": event_type, "stream_session_id": stream_session_id, **{
            key: metadata[key] for key in ("correlation_id", "streamer_id", "job_id", "service")
            if metadata and key in metadata
        }},
    )
    db.add(
        SystemEvent(
            event_type=event_type[:64],
            severity=severity,
            stream_session_id=stream_session_id,
            message=safe or "障害",
            metadata_json=meta,
            correlation_id=metadata.get("correlation_id") if metadata else None,
            created_at=utcnow(),
        )
    )


def search_comments(
    db: Session,
    *,
    text: str = "",
    author: str = "",
    author_id: str = "",
    author_name: str = "",
    streamer_id: int | None = None,
    platform: str = "",
    session_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Comment], int]:
    stmt: Select[tuple[Comment]] = select(Comment).join(
        StreamSession, Comment.stream_session_id == StreamSession.id
    ).join(Streamer, StreamSession.streamer_id == Streamer.id)
    if text:
        stmt = stmt.where(Comment.message.contains(text, autoescape=True))
    if author:
        stmt = stmt.where(
            or_(
                Comment.platform_user_id.contains(author, autoescape=True),
                Comment.nickname.contains(author, autoescape=True),
            )
        )
    if author_id:
        stmt = stmt.where(Comment.platform_user_id.contains(author_id, autoescape=True))
    if author_name:
        stmt = stmt.where(Comment.nickname.contains(author_name, autoescape=True))
    moment = func.coalesce(Comment.posted_at, Comment.received_at)
    if streamer_id:
        stmt = stmt.where(Streamer.id == streamer_id)
    if platform:
        stmt = stmt.where(Streamer.platform == platform)
    if session_id:
        stmt = stmt.where(Comment.stream_session_id == session_id)
    if start:
        stmt = stmt.where(moment >= start)
    if end:
        stmt = stmt.where(moment < end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.order_by(moment.asc(), Comment.id.asc())
            .offset(max(page - 1, 0) * page_size)
            .limit(page_size)
        )
    )
    return rows, int(total)


def search_sessions(
    db: Session,
    *,
    text: str = "",
    platform: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    streamer_id: int | None = None,
    recording_status: str = "",
    ascending: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> list[StreamSession]:
    stmt = select(StreamSession).join(Streamer, StreamSession.streamer_id == Streamer.id)
    if text:
        stmt = stmt.where(
            or_(
                Streamer.platform_user_id.contains(text),
                Streamer.display_name.contains(text),
                StreamSession.title.contains(text),
            )
        )
    if platform:
        stmt = stmt.where(Streamer.platform == platform)
    if start:
        stmt = stmt.where(StreamSession.detected_at >= start)
    if end:
        stmt = stmt.where(StreamSession.detected_at < end)
    if streamer_id is not None:
        stmt = stmt.where(StreamSession.streamer_id == streamer_id)
    if recording_status:
        latest_status = (
            select(RecordingJob.status)
            .where(RecordingJob.stream_session_id == StreamSession.id)
            .order_by(RecordingJob.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        degraded = or_(
            select(RecordingJob.id).where(
                RecordingJob.stream_session_id == StreamSession.id,
                RecordingJob.status.in_(("FAILED", "PARTIAL")),
            ).exists(),
            select(RecordingSegment.id).where(
                RecordingSegment.stream_session_id == StreamSession.id,
                RecordingSegment.validation_status != "valid",
            ).exists(),
            select(CollectionGap.id).where(
                CollectionGap.stream_session_id == StreamSession.id, CollectionGap.job_type == "recording",
            ).exists(),
        )
        if recording_status == "none":
            stmt = stmt.where(latest_status.is_(None))
        elif recording_status == "PARTIAL":
            stmt = stmt.where(degraded)
        elif recording_status == "COMPLETED":
            stmt = stmt.where(latest_status == recording_status, ~degraded)
        else:
            stmt = stmt.where(latest_status == recording_status)

    ordering = (StreamSession.detected_at, StreamSession.id)
    if not ascending:
        ordering = tuple(column.desc() for column in ordering)
    return list(db.scalars(stmt.order_by(*ordering).offset(offset).limit(limit)))


def collection_label(job: CommentCollectionJob | None) -> str:
    if job is None or job.status in {"PENDING", "CONNECTING"}:
        return "未収集"
    if job.status == "DISABLED":
        return "無効"
    if not job.stored_count and job.status in {"FAILED", "PARTIAL", "RECONNECTING"}:
        return "取得できない区間あり"
    return str(job.stored_count)


def recording_summary(db: Session, session_id: int) -> dict:
    jobs = list(db.scalars(select(RecordingJob).where(
        RecordingJob.stream_session_id == session_id,
    ).order_by(RecordingJob.id.desc())))
    segments = list(db.scalars(select(RecordingSegment).where(RecordingSegment.stream_session_id == session_id)))
    gaps = db.scalar(select(func.count(CollectionGap.id)).where(
        CollectionGap.stream_session_id == session_id, CollectionGap.job_type == "recording",
    ))
    valid = [segment for segment in segments if segment.validation_status == "valid" and not segment.files_deleted_at]
    invalid = len([segment for segment in segments if segment.validation_status != "valid"])
    failed = len([job for job in jobs if job.status in {"FAILED", "PARTIAL"}])
    state = jobs[0].status if jobs else None
    return {
        "latest_recording_status": state,
        "recording_status": "PARTIAL" if failed or gaps or invalid else state,
        "recording_gap_count": gaps,
        "unverified_segment_count": invalid,
        "failed_recording_jobs": failed,
        "file_count": len(valid) if jobs else None,
        "file_size": sum(segment.file_size for segment in valid) if valid and all(
            segment.file_size is not None for segment in valid
        ) else None,
        "duration_ms": sum(segment.duration_ms for segment in valid) if valid and all(
            segment.duration_ms is not None for segment in valid
        ) else None,
    }
