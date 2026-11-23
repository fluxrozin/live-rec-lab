"""管理ファイルと履歴の台帳付き削除。"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import (
    CollectionGap,
    Comment,
    CommentCollectionJob,
    DeletionOperation,
    RecordingJob,
    RecordingSegment,
    StreamSession,
    SystemEvent,
)
from live_rec_lab.db.queries import add_event
from live_rec_lab.db.session import session_scope
from live_rec_lab.monitoring.state import ACTIVE_COMMENTS, ACTIVE_RECORDING, OPEN_SESSION
from live_rec_lab.services.media_files import check_identity, configured_root, file_identity, managed_path

_lock = RLock()
_MODES = {"files", "files_and_history", "auto_files", "comment_expiry"}


@dataclass(frozen=True)
class DeleteResult:
    id: int
    mode: str
    status: str
    error: str | None


def deletion_pending(db: Session, session_id: int) -> bool:
    return db.scalar(select(DeletionOperation.id).where(
        DeletionOperation.target_type == "stream_session",
        DeletionOperation.target_id == session_id,
        DeletionOperation.status != "completed",
    ).limit(1)) is not None


def _eligible(db: Session, stream: StreamSession, *, comments_only: bool) -> None:
    if stream.status in OPEN_SESSION or stream.ended_at is None:
        raise ValueError("配信の停止と確定を確認してから削除してください")
    for model, statuses in ((RecordingJob, ACTIVE_RECORDING), (CommentCollectionJob, ACTIVE_COMMENTS)):
        if db.scalar(select(model.id).where(model.stream_session_id == stream.id, model.status.in_(statuses)).limit(1)):
            raise ValueError("実行中または復旧中のジョブがあります")
    if not comments_only and db.scalar(select(RecordingSegment.id).where(
        RecordingSegment.stream_session_id == stream.id,
        RecordingSegment.files_deleted_at.is_(None),
        (
            (RecordingSegment.validation_status != "valid")
            | RecordingSegment.temp_path.is_not(None)
            | RecordingSegment.srt_stage_path.is_not(None)
        ),
    ).limit(1)):
        raise ValueError("未確定または復旧中のファイルがあります")


def _prepare(db: Session, stream: StreamSession, mode: str) -> DeletionOperation:
    current = db.scalar(select(DeletionOperation).where(
        DeletionOperation.target_id == stream.id, DeletionOperation.status != "completed",
    ).order_by(DeletionOperation.id))
    if current:
        if current.mode != mode:
            raise ValueError("別の削除操作を復旧中です")
        return current

    comments_only = mode == "comment_expiry"
    _eligible(db, stream, comments_only=comments_only)
    rows = list(db.scalars(select(RecordingSegment).where(RecordingSegment.stream_session_id == stream.id)))
    files = []
    for row in rows:
        if not comments_only and row.file_path and row.files_deleted_at is None:
            files.append((row.file_path, row.file_device, row.file_inode))
        # Include a staged publication so that neither a .part file nor a just-published subtitle survives expiry.
        if row.srt_stage_path:
            files.append((row.srt_stage_path, row.srt_stage_device, row.srt_stage_inode))
            if row.file_path:
                final_srt = str(Path(row.file_path).with_suffix(".srt"))
                files.append((final_srt, row.srt_stage_device, row.srt_stage_inode))
        if row.srt_path:
            files.append((row.srt_path, row.srt_device, row.srt_inode))
    operation = DeletionOperation(
        target_id=stream.id, mode=mode, status="prepared", target_paths=json.dumps(files, ensure_ascii=False),
    )
    db.add(operation)
    db.flush()
    return operation


def _delete_paths(operation_id: int) -> None:
    with session_scope(write_lock=True) as db:
        operation = db.get(DeletionOperation, operation_id)
        files = json.loads(operation.target_paths)
        if not files:
            return
        root = configured_root(db)
        # Check all remaining targets first. A previous iteration may already have removed some.
        expected = {}
        for name, device, inode in files:
            path = managed_path(root, name)
            expected.setdefault(path, set()).add((device, inode))
        verified = []
        for path, identities in expected.items():
            try:
                identity = file_identity(path)
            except FileNotFoundError:
                continue
            if identity not in identities:
                raise ValueError("削除対象の実体が記録と一致しません")
            verified.append((path, *identity))
        for path, device, inode in verified:
            check_identity(path, device, inode)
            path.unlink()



def _drop_history(db: Session, session_id: int) -> None:
    for model in (RecordingSegment, RecordingJob, Comment, CollectionGap, CommentCollectionJob):
        db.execute(delete(model).where(model.stream_session_id == session_id))
    db.execute(update(SystemEvent).where(SystemEvent.stream_session_id == session_id).values(stream_session_id=None))
    db.execute(delete(StreamSession).where(StreamSession.id == session_id))


def _finish(db: Session, operation: DeletionOperation) -> None:
    stream = db.get(StreamSession, operation.target_id)
    now = utcnow()
    if stream is None:
        raise ValueError("削除対象の配信を照合できません")
    if operation.mode == "files_and_history":
        _drop_history(db, stream.id)
    else:
        comments_only = operation.mode == "comment_expiry"
        segments = db.scalars(select(RecordingSegment).where(RecordingSegment.stream_session_id == stream.id))
        for segment in segments:
            segment.srt_path = None
            segment.srt_device = None
            segment.srt_inode = None
            segment.srt_stage_path = None
            segment.srt_stage_device = None
            segment.srt_stage_inode = None
            segment.srt_stage_version = None
            segment.srt_provisional = False
            if comments_only:
                segment.srt_status = "expired"
            elif segment.file_path is not None:
                segment.files_deleted_at = segment.files_deleted_at or now
                segment.srt_status = "files_deleted"
        if comments_only:
            count = db.scalar(select(func.count(Comment.id)).where(Comment.stream_session_id == stream.id))
            db.execute(delete(Comment).where(Comment.stream_session_id == stream.id))
            stream.comments_purged_at = now
            stream.purged_comment_count = count
    operation.finished_at = now
    operation.last_error = None
    operation.status = "completed"
    add_event(
        db, operation.mode, "info", "削除を確定しました",
        stream.id if operation.mode != "files_and_history" else None,
        metadata={"operation_id": operation.id},
    )


def _execute(operation_id: int) -> DeleteResult:
    with session_scope(write_lock=True) as db:
        operation = db.get(DeletionOperation, operation_id)
        if operation.status == "completed":
            return DeleteResult(operation.id, operation.mode, operation.status, None)
        operation.status = "running"
    try:
        _delete_paths(operation_id)
        with session_scope(write_lock=True) as db:
            operation = db.get(DeletionOperation, operation_id)
            _finish(db, operation)
            return DeleteResult(operation.id, operation.mode, operation.status, None)
    except (OSError, ValueError) as error:
        with session_scope(write_lock=True) as db:
            operation = db.get(DeletionOperation, operation_id)
            operation.status = "partial"
            reason = str(error)
            if operation.last_error != reason:
                add_event(db, operation.mode, "warning", reason, operation.target_id,
                          metadata={"operation_id": operation.id})
            operation.last_error = reason
            return DeleteResult(operation.id, operation.mode, operation.status, operation.last_error)


def delete_session(session_id: int, mode: str) -> DeleteResult:
    if mode not in _MODES:
        raise ValueError("ファイルのみ、またはファイルと履歴の削除を選択してください")
    with _lock:
        with session_scope(write_lock=True) as db:
            stream = db.get(StreamSession, session_id)
            if stream is None:
                raise LookupError("対象の配信がありません")
            operation_id = _prepare(db, stream, mode).id
        return _execute(operation_id)


def resume_operations() -> bool:
    with _lock:
        with session_scope() as db:
            ids = list(db.scalars(select(DeletionOperation.id).where(DeletionOperation.status != "completed")))
        results = [_execute(operation_id) for operation_id in ids]
        return all(result.status == "completed" for result in results)


def run_retention(settings: dict[str, object], now: datetime | None = None) -> bool:
    now = now or utcnow()
    comments_days = int(settings.get("comment_retention_days", 0))
    video_days = int(settings.get("retention_days", 0))
    auto_files = bool(settings.get("auto_delete_recordings", False))
    if not comments_days and not auto_files:
        return True

    with session_scope() as db:
        sessions = list(db.scalars(select(StreamSession).where(
            StreamSession.ended_at.is_not(None), StreamSession.status.not_in(OPEN_SESSION),
        ).order_by(StreamSession.ended_at)))
    healthy = True
    for stream in sessions:
        ended = stream.ended_at.replace(tzinfo=now.tzinfo)
        if comments_days and stream.comments_purged_at is None and now - ended >= timedelta(days=comments_days):
            try:
                result = delete_session(stream.id, "comment_expiry")
                healthy &= result.status == "completed"
            except (ValueError, LookupError):
                # An active queue, a concurrent manual deletion or a recovery operation can postpone expiry.
                healthy = False
        if not auto_files:
            continue
        with session_scope() as db:
            held = db.scalar(select(func.sum(RecordingSegment.file_size)).where(
                RecordingSegment.stream_session_id == stream.id,
                RecordingSegment.validation_status == "valid",
                RecordingSegment.files_deleted_at.is_(None),
            ))
            if not held:
                continue
            total_limit = int(settings["max_recording_bytes"])
            total = db.scalar(select(func.sum(RecordingSegment.file_size)).where(
                RecordingSegment.validation_status == "valid", RecordingSegment.files_deleted_at.is_(None),
            )) if total_limit else None
            try:
                low_space = shutil.disk_usage(configured_root(db)).free < int(settings["min_free_bytes"])
            except (ValueError, OSError):
                low_space = False
        if (video_days and now - ended >= timedelta(days=video_days)) or (total and total > total_limit) or low_space:
            try:
                result = delete_session(stream.id, "auto_files")
                healthy &= result.status == "completed"
            except (ValueError, LookupError):
                continue
    return healthy
