"""セグメント単位の SRT。"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import CollectionGap, Comment, CommentCollectionJob, RecordingSegment, StreamSession
from live_rec_lab.db.queries import load_settings
from live_rec_lab.db.session import session_scope
from live_rec_lab.monitoring.state import ACTIVE_COMMENTS
from live_rec_lab.services.media_files import check_identity, configured_root, file_identity, managed_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def format_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _clean(text: str) -> str:
    flattened = text.replace("\r\n", "\n").replace("\r", "\n")
    flattened = flattened.replace("-->", "→")
    return "\n".join(line if line.strip() else "\u00a0" for line in flattened.split("\n"))


def cue_text(nickname: str | None, anonymous_label: str | None, message: str) -> str:
    who = anonymous_label or nickname or ""
    body = _clean(message)
    if who:
        return f"{_clean(who)}: {body}"
    return body


def render_srt(cues: list[Cue]) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def build_cues(
    comments: list[Comment],
    *,
    anchor: datetime | None,
    duration_ms: int | None,
    display_seconds: float,
    gaps: list[tuple[datetime, datetime]] | None = None,
) -> list[Cue] | None:
    if anchor is None:
        return None
    cues: list[Cue] = []
    ordered = sorted(comments, key=lambda item: (_utc(item.posted_at or item.received_at), item.id))
    for comment in ordered:
        moment = _utc(comment.posted_at or comment.received_at)
        if gaps and any(_utc(begin) <= moment < _utc(end) for begin, end in gaps):
            continue
        start = (moment - _utc(anchor)).total_seconds()
        if start < 0:
            continue
        end = start + display_seconds
        if duration_ms is not None:
            limit = duration_ms / 1000
            if start >= limit:
                continue
            end = min(end, limit)
        if end <= start:
            continue
        cues.append(Cue(start, end, cue_text(comment.nickname, comment.anonymous_label, comment.message)))
    return cues


def aware(value: datetime, fallback: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=fallback.tzinfo)
    return value


def offset_ms(origin: datetime | None, moment: datetime | None, *, allow_negative: bool) -> int | None:
    if origin is None or moment is None:
        return None
    moment = aware(moment, origin)
    origin = aware(origin, moment)
    delta = moment - origin
    if delta < timedelta(0) and not allow_negative:
        return None
    return int(delta.total_seconds() * 1000)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def segment_anchor(segment: RecordingSegment) -> datetime | None:
    if segment.sync_method == "unsynced":
        return None
    anchor = segment.first_media_at or segment.started_at
    if anchor is None:
        return None
    return _utc(anchor) - timedelta(milliseconds=segment.srt_manual_offset_ms or 0)


def _blocked(db: Session, segment: RecordingSegment) -> bool:
    from live_rec_lab.comments.store import comment_writes_blocked

    stream = db.get(StreamSession, segment.stream_session_id)
    return stream is None or comment_writes_blocked(db, stream)


def _destination(db: Session, segment: RecordingSegment) -> tuple[Path, Path]:
    if not segment.file_path or segment.validation_status != "valid" or segment.files_deleted_at is not None:
        raise ValueError("確定済み動画が必要です")
    root = configured_root(db)
    video = managed_path(root, segment.file_path)
    check_identity(video, segment.file_device, segment.file_inode)
    if video.stat().st_size != segment.file_size:
        raise ValueError("確定済み動画のサイズが記録と一致しません")
    return root, managed_path(root, video.with_suffix(".srt"))


def _clear_stage(segment: RecordingSegment) -> None:
    segment.srt_stage_path = None
    segment.srt_stage_version = None
    segment.srt_stage_device = None
    segment.srt_stage_inode = None


def _record_publication(segment: RecordingSegment, target: Path, identity: tuple[int, int]) -> None:
    segment.srt_path = str(target)
    segment.srt_device, segment.srt_inode = identity


def _recover_publication(segment: RecordingSegment, root: Path, target: Path) -> None:
    if not segment.srt_stage_path:
        return
    stage = managed_path(root, segment.srt_stage_path)
    identity = (segment.srt_stage_device, segment.srt_stage_inode)
    try:
        published = file_identity(target) == identity
    except FileNotFoundError:
        published = False
    if published:
        _record_publication(segment, target, identity)
        segment.srt_generated_from_version = segment.srt_stage_version
        segment.srt_generated_at = utcnow()
    try:
        check_identity(stage, *identity)
        stage.unlink()
    except FileNotFoundError:
        pass
    segment.srt_status = "stale"
    _clear_stage(segment)


def _provisional(db: Session, session_id: int) -> bool:
    return db.scalar(
        select(CommentCollectionJob.id)
        .where(
            CommentCollectionJob.stream_session_id == session_id,
            CommentCollectionJob.status.in_(ACTIVE_COMMENTS),
        ).limit(1)
    ) is not None


def _prepare(segment_id: int, manual_offset_ms: int | None) -> tuple[int, Path, str]:
    with session_scope(write_lock=True) as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is None or _blocked(db, segment):
            raise ValueError("削除処理中または期限削除済みの字幕です")
        root, target = _destination(db, segment)
        _recover_publication(segment, root, target)
        if manual_offset_ms is not None and segment.srt_manual_offset_ms != manual_offset_ms:
            segment.srt_manual_offset_ms = manual_offset_ms
            segment.srt_input_version += 1
        if manual_offset_ms is not None:
            segment.sync_method = "manual"
        elif segment.sync_method is None:
            segment.sync_method = "approximate"

    # A WAL read transaction gives the version and its comment rows one consistent snapshot.
    with session_scope() as db:
        db.connection().exec_driver_sql("BEGIN")
        segment = db.get(RecordingSegment, segment_id)
        if segment is None or _blocked(db, segment):
            raise ValueError("削除処理中または期限削除済みの字幕です")
        if _destination(db, segment)[1] != target:
            raise ValueError("字幕の保存先が変更されました")
        anchor = segment_anchor(segment)
        duration_ms = segment.duration_ms
        if anchor is None or not duration_ms:
            raise ValueError("字幕の時刻を決める基準がありません")
        version = segment.srt_input_version
        display_seconds = float(load_settings(db)["srt_display_seconds"])
        end = anchor + timedelta(milliseconds=duration_ms)
        moment = func.coalesce(Comment.posted_at, Comment.received_at)
        comments = list(db.scalars(select(Comment).where(
            Comment.stream_session_id == segment.stream_session_id, moment >= anchor, moment < end,
        )))
        gaps = list(db.execute(select(CollectionGap.started_at, CollectionGap.ended_at).where(
            CollectionGap.stream_session_id == segment.stream_session_id,
            CollectionGap.job_type == "recording",
            CollectionGap.started_at < end,
            CollectionGap.ended_at > anchor,
        )))
    cues = build_cues(
        comments, anchor=anchor, duration_ms=duration_ms, display_seconds=display_seconds, gaps=gaps,
    ) or []
    return version, target, render_srt(cues)


def _remove_previous(segment: RecordingSegment, target: Path) -> None:
    if segment.srt_path:
        if Path(segment.srt_path) != target:
            raise ValueError("以前の字幕と保存先が一致しません")
        try:
            check_identity(target, segment.srt_device, segment.srt_inode)
            target.unlink()
        except FileNotFoundError:
            pass
    elif target.exists() or target.is_symlink():
        raise ValueError("同名の管理外字幕があります")


def _publish(segment_id: int, version: int, target: Path, stage: Path | None) -> str:
    from live_rec_lab.storage import publish_exclusive

    identity = file_identity(stage) if stage else None
    if stage:
        with session_scope(write_lock=True) as db:
            segment = db.get(RecordingSegment, segment_id)
            if segment is None or _blocked(db, segment):
                raise ValueError("字幕を生成できないセッションです")
            segment.srt_stage_path = str(stage)
            segment.srt_stage_device, segment.srt_stage_inode = identity
            segment.srt_stage_version = version

    with session_scope(write_lock=True) as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is None or _blocked(db, segment):
            raise ValueError("字幕を生成できないセッションです")
        if segment.srt_input_version != version or _destination(db, segment)[1] != target:
            segment.srt_status = "stale"
            _clear_stage(segment)
            return "stale"
        if stage:
            check_identity(stage, *identity)
            if segment.srt_path and target.exists():
                if Path(segment.srt_path) != target:
                    raise ValueError("以前の字幕と保存先が一致しません")
                check_identity(target, segment.srt_device, segment.srt_inode)
                os.replace(stage, target)
            else:
                publish_exclusive(stage, target)
            _record_publication(segment, target, identity)
        else:
            _remove_previous(segment, target)
            segment.srt_path = None
            segment.srt_device = None
            segment.srt_inode = None
        # Keep the staging ledger until the version is checked again after publication.
        segment.srt_status = "stale"

    return _complete_publication(segment_id, version, has_subtitle=stage is not None)


def _complete_publication(segment_id: int, version: int, *, has_subtitle: bool) -> str:
    with session_scope(write_lock=True) as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is None:
            raise ValueError("字幕の公開結果を照合できません")
        segment.srt_generated_from_version = version
        segment.srt_generated_at = utcnow() if has_subtitle else None
        matching = not _blocked(db, segment) and segment.srt_input_version == version
        status = ("generated" if has_subtitle else "skipped") if matching else "stale"
        segment.srt_status = status
        segment.srt_error = None
        if matching:
            segment.srt_provisional = _provisional(db, segment.stream_session_id)
        _clear_stage(segment)
        return status


def generate_segment_srt(segment_id: int, *, manual_offset_ms: int | None = None) -> str:
    # One generator per segment because staging rows are committed before filesystem operations.
    with _generation_lock(segment_id):
        stage = None
        stage_identity = None
        published = False
        try:
            version, target, rendered = _prepare(segment_id, manual_offset_ms)
            if rendered:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=target.parent, prefix=".srt-", suffix=".part", delete=False,
                ) as output:
                    stage = Path(output.name)
                    stage_identity = file_identity(stage)
                    output.write(rendered)
                    output.flush()
                    os.fsync(output.fileno())
            status = _publish(segment_id, version, target, stage)
            published = True
            return status
        except (OSError, ValueError) as error:
            with session_scope(write_lock=True) as db:
                segment = db.get(RecordingSegment, segment_id)
                if segment is not None and not _blocked(db, segment):
                    segment.srt_status = "resync_failed" if segment.srt_path else "failed"
                    segment.srt_error = str(error)
            log.warning("字幕を確定できません: %s", error, extra={"event": "srt_failed", "segment_id": segment_id})
            return "failed"
        finally:
            if stage is not None and stage_identity is not None:
                with session_scope() as db:
                    row = db.get(RecordingSegment, segment_id)
                    recorded = bool(row and row.srt_stage_path == str(stage))
                if published or not recorded:
                    try:
                        check_identity(stage, *stage_identity)
                        stage.unlink()
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError) as error:
                        log.warning("字幕の一時ファイルを回収できません: %s", error)


_generation_locks = tuple(Lock() for _ in range(64))


def _generation_lock(segment_id: int):
    return _generation_locks[segment_id % len(_generation_locks)]


def resync_session(stream_session_id: int) -> None:
    with session_scope() as db:
        candidates = list(db.scalars(select(RecordingSegment.id).where(
            RecordingSegment.stream_session_id == stream_session_id,
            RecordingSegment.validation_status == "valid",
            RecordingSegment.files_deleted_at.is_(None),
            RecordingSegment.srt_status != "expired",
            (
                RecordingSegment.srt_generated_from_version.is_(None)
                | (RecordingSegment.srt_generated_from_version != RecordingSegment.srt_input_version)
                | RecordingSegment.srt_provisional
                | (RecordingSegment.srt_stage_path.is_not(None))
                | (RecordingSegment.srt_status == "stale")
            ),
        )))
    for segment_id in candidates:
        for _ in range(3):
            if generate_segment_srt(segment_id) != "stale":
                break
