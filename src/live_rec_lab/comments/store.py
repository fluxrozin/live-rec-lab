"""コメント原文、取得時刻、字幕入力の永続化。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from live_rec_lab.comments.srt import offset_ms, segment_anchor
from live_rec_lab.config import utcnow
from live_rec_lab.db.models import (
    Comment,
    CommentCollectionJob,
    DeletionOperation,
    RecordingJob,
    RecordingSegment,
    StreamSession,
)
from live_rec_lab.db.session import session_scope
from live_rec_lab.platforms.base import CommentIn


def comment_writes_blocked(db: Session, stream: StreamSession) -> bool:
    return stream.comments_purged_at is not None or db.scalar(
        select(DeletionOperation.id)
        .where(DeletionOperation.target_id == stream.id, DeletionOperation.status != "completed")
        .limit(1)
    ) is not None


def _store_batch(
    db: Session, stream: StreamSession, job: CommentCollectionJob | None, items: Sequence[CommentIn],
) -> int:
    if comment_writes_blocked(db, stream):
        raise ValueError("削除処理中またはコメント保存期限により削除済みです")
    recording_anchor = db.scalar(select(RecordingJob.started_at).where(
        RecordingJob.stream_session_id == stream.id, RecordingJob.started_at.is_not(None),
    ).order_by(RecordingJob.started_at).limit(1))
    stored_at = utcnow()
    receipts = []
    moments: list[datetime] = []
    for item in items:
        received = item.received_at or stored_at
        receipts.append(received if received.tzinfo else received.replace(tzinfo=stored_at.tzinfo))
        if not item.message:
            continue
        moment = item.posted_at or received
        result = db.execute(insert(Comment).values(
            stream_session_id=stream.id,
            platform_comment_id=item.platform_comment_id or None,
            platform_user_id=item.platform_user_id,
            nickname=item.nickname,
            is_anonymous=item.is_anonymous,
            anonymous_label=item.anonymous_label or None,
            comment_source=item.source,
            message=item.message,
            posted_at=item.posted_at,
            received_at=received,
            stream_offset_ms=offset_ms(stream.platform_started_at, moment, allow_negative=True),
            recording_offset_ms=offset_ms(recording_anchor, moment, allow_negative=False),
            created_at=stored_at,
        ).on_conflict_do_nothing(index_elements=["stream_session_id", "platform_comment_id"]))
        if result.rowcount:
            moments.append(moment)
    if moments:
        segments = db.scalars(select(RecordingSegment).where(
            RecordingSegment.stream_session_id == stream.id,
            RecordingSegment.validation_status == "valid",
            RecordingSegment.files_deleted_at.is_(None),
        ))
        for segment in segments:
            anchor = segment_anchor(segment)
            if anchor is None or not segment.duration_ms:
                continue
            end = anchor + timedelta(milliseconds=segment.duration_ms)
            if any(anchor <= (moment if moment.tzinfo else moment.replace(tzinfo=anchor.tzinfo)) < end
                   for moment in moments):
                segment.srt_input_version += 1
                if segment.srt_generated_from_version is not None:
                    segment.srt_status = "stale"
    if job is not None and items:
        job.received_count += len(items)
        job.stored_count += len(moments)
        latest = job.last_received_at
        if latest is not None:
            receipts.append(latest if latest.tzinfo else latest.replace(tzinfo=stored_at.tzinfo))
        job.last_received_at = max(receipts)
        job.updated_at = stored_at
    return len(moments)


def store_comment(db: Session, session_id: int, item: CommentIn) -> bool:
    stream = db.get(StreamSession, session_id)
    if stream is None:
        return False
    job = db.scalar(
        select(CommentCollectionJob)
        .where(CommentCollectionJob.stream_session_id == session_id)
        .order_by(CommentCollectionJob.id.desc()).limit(1)
    )
    return bool(_store_batch(db, stream, job, [item]))


def save_comments(session_id: int, job_id: int, items: Sequence[CommentIn]) -> int:
    with session_scope(write_lock=True) as db:
        stream = db.get(StreamSession, session_id)
        job = db.get(CommentCollectionJob, job_id)
        if stream is None or job is None or job.stream_session_id != session_id:
            raise ValueError("配信とコメント収集ジョブを照合できません")
        return _store_batch(db, stream, job, items)
