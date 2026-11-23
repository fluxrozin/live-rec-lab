"""起動前に録画の予約・一時ファイル・正式ファイルを照合する。"""

from __future__ import annotations

import stat
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import CommentCollectionJob, RecordingJob, RecordingSegment, RuntimeInstance
from live_rec_lab.db.queries import add_event, load_settings
from live_rec_lab.monitoring.state import ACTIVE_COMMENTS, ACTIVE_RECORDING
from live_rec_lab.recording.manager import _finalize_open, add_gap
from live_rec_lab.recording.segments import clean_published_staging
from live_rec_lab.storage import recording_root_status, safe_managed_path


def _command_holds(token: str) -> bool:
    proc = Path("/proc")
    if not proc.is_dir() or not token:
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if token.encode() in raw.split(b"\x00"):
            return True
    return False


def _valid_media_issue(segment: RecordingSegment, root: Path) -> str | None:
    if not segment.file_path:
        return "確定済み動画のパスがありません"
    try:
        path = safe_managed_path(root, Path(segment.file_path))
        current = path.lstat()
    except (ValueError, OSError):
        return "確定済み動画を確認できません"
    if not stat.S_ISREG(current.st_mode) or not current.st_size or current.st_size != segment.file_size:
        return "確定済み動画の種類、サイズまたは状態が記録と一致しません"
    if segment.file_device is None or segment.file_inode is None:
        return "動画の所有情報を確認できません"
    if (current.st_dev, current.st_ino) != (segment.file_device, segment.file_inode):
        return "確定済み動画の実体が記録と一致しません"
    return None


def recover(db: Session, *, root: Path | None = None) -> set[int]:
    """起動中の負荷を閉じた状態で実行。戻り値の各配信はSRT再同期できる。"""
    now = utcnow()
    resync: set[int] = set()
    for instance in db.scalars(select(RuntimeInstance).where(RuntimeInstance.status == "running")):
        instance.status = "stopped"
        instance.stopped_at = now
    for job in db.scalars(select(CommentCollectionJob).where(CommentCollectionJob.status.in_(ACTIVE_COMMENTS))):
        job.status = "PARTIAL"
        job.finished_at = now
        job.updated_at = now
        job.end_reason = "app_interrupted"
        resync.add(job.stream_session_id)
        add_gap(db, job.stream_session_id, "comments", "前回のコメント収集が中断しました",
                started_at=job.last_received_at or job.started_at)
        add_event(db, "comments_recovered", "warning", "再起動時にコメント収集を中断として記録しました",
                  job.stream_session_id, {"job_id": job.id})
    if root is None:
        configured = str(load_settings(db)["recording_root"])
        try:
            root = recording_root_status(Path(configured)) if configured else None
        except (ValueError, OSError):
            root = None
    if root is not None and not root.is_dir():
        root = None
    db.commit()

    pending_ids = select(RecordingSegment.recording_job_id).where(RecordingSegment.validation_status == "pending")
    job_ids = list(db.scalars(select(RecordingJob.id).where(or_(
        RecordingJob.status.in_(ACTIVE_RECORDING), RecordingJob.id.in_(pending_ids),
    ))))
    for job_id in job_ids:
        job = db.get(RecordingJob, job_id)
        if job is None:
            continue
        rows = list(db.scalars(select(RecordingSegment).where(RecordingSegment.recording_job_id == job_id)))
        candidates = [row for row in rows if row.validation_status == "pending"]
        if any(_command_holds(segment.temp_path or "") for segment in candidates):
            add_event(db, "orphan_recorder", "warning", "前回の録画プロセスが残っています",
                      job.stream_session_id, {"job_id": job.id})
            db.commit()
            continue
        if candidates and root is None:
            add_event(db, "recording_recovery_held", "warning", "録画先を確認できず未確定録画の復旧を保留しています",
                      job.stream_session_id, {"job_id": job.id})
            db.commit()
            continue
        session_id = job.stream_session_id
        any_valid = any(row.validation_status == "valid" for row in rows)
        if root is not None:
            segment_ids = [segment.id for segment in candidates]
            db.commit()
            for segment_id in segment_ids:
                any_valid |= _finalize_open(segment_id, session_id, root) is True
            db.expire_all()
            job = db.get(RecordingJob, job_id)
        if job is None:
            continue
        if not (job.status == "CANCELLED" and job.manual_stop_requested and not any_valid):
            job.status = "PARTIAL" if any_valid else "FAILED"
            job.end_reason = "app_interrupted"
        job.finished_at = job.finished_at or now
        job.updated_at = now
        add_gap(db, session_id, "recording", "前回の録画が中断しました", started_at=job.started_at)
        add_event(db, "recording_recovered", "warning", "再起動時に録画を照合しました", session_id, {"job_id": job.id})
        if any_valid:
            resync.add(session_id)
        db.commit()

    if root is not None:
        for segment in db.scalars(select(RecordingSegment).where(
            RecordingSegment.validation_status == "valid", RecordingSegment.files_deleted_at.is_(None),
        )):
            problem = _valid_media_issue(segment, root)
            if problem:
                add_event(db, "recording_media_mismatch", "warning", problem, segment.stream_session_id,
                          {"job_id": segment.recording_job_id})
                continue
            if (segment.srt_stage_path or segment.srt_provisional
                    or segment.srt_generated_from_version != segment.srt_input_version):
                resync.add(segment.stream_session_id)
            if segment.temp_path:
                try:
                    safe_managed_path(root, Path(segment.temp_path))
                    clean_published_staging(segment)
                except (OSError, ValueError):
                    add_event(db, "recording_cleanup_held", "warning", "録画の一時ファイルの処理を保留しています",
                              segment.stream_session_id, {"job_id": segment.recording_job_id})
    return resync
