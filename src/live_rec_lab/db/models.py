"""SQLite のテーブル。制約の意味は data-model.md が正本。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    from live_rec_lab.config import utcnow

    return utcnow()


class Streamer(Base):
    __tablename__ = "streamers"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    profile_url: Mapped[str | None] = mapped_column(String(300))
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    comments_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    live_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StreamSession(Base):
    __tablename__ = "stream_sessions"
    __table_args__ = (UniqueConstraint("streamer_id", "platform_stream_id"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), nullable=False)
    platform_stream_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    platform_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    platform_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    orphan_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(128))
    last_seen_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_segment_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    filename_disambiguator: Mapped[str | None] = mapped_column(String(40))
    filename_stem: Mapped[str | None] = mapped_column(String(200))
    comments_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    manual_stop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RecordingJob(Base):
    __tablename__ = "recording_jobs"
    __table_args__ = (UniqueConstraint("id", "stream_session_id", name="uq_recording_job_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    recorder_engine: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    end_reason: Mapped[str | None] = mapped_column(String(128))
    manual_stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RecordingSegment(Base):
    __tablename__ = "recording_segments"
    __table_args__ = (
        UniqueConstraint("stream_session_id", "segment_number", name="uq_recording_segment_number"),
        ForeignKeyConstraint(
            ["recording_job_id", "stream_session_id"],
            ["recording_jobs.id", "recording_jobs.stream_session_id"],
            name="fk_recording_segment_job_session",
        ),
        Index("ix_recording_segments_job", "recording_job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_job_id: Mapped[int] = mapped_column(Integer, nullable=False)
    stream_session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_number: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temp_path: Mapped[str | None] = mapped_column(String(500))
    reserved_path: Mapped[str | None] = mapped_column(String(500))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_device: Mapped[int | None] = mapped_column(Integer)
    file_inode: Mapped[int | None] = mapped_column(Integer)
    file_size: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    first_media_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    media_start_pts: Mapped[float | None] = mapped_column(Float)
    sync_offset_ms: Mapped[int | None] = mapped_column(Integer)
    sync_method: Mapped[str | None] = mapped_column(String(16))
    container_format: Mapped[str | None] = mapped_column(String(16))
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    srt_path: Mapped[str | None] = mapped_column(String(500))
    srt_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_generated")
    srt_error: Mapped[str | None] = mapped_column(String(500))
    srt_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    srt_input_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    srt_generated_from_version: Mapped[int | None] = mapped_column(Integer)
    srt_provisional: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    srt_device: Mapped[int | None] = mapped_column(Integer)
    srt_inode: Mapped[int | None] = mapped_column(Integer)
    srt_stage_path: Mapped[str | None] = mapped_column(String(500))
    srt_stage_device: Mapped[int | None] = mapped_column(Integer)
    srt_stage_inode: Mapped[int | None] = mapped_column(Integer)
    srt_stage_version: Mapped[int | None] = mapped_column(Integer)
    srt_manual_offset_ms: Mapped[int | None] = mapped_column(Integer)
    files_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CommentCollectionJob(Base):
    __tablename__ = "comment_collection_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stored_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    end_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("stream_session_id", "platform_comment_id"),
        Index("ix_comments_session_received", "stream_session_id", "received_at"),
        Index("ix_comments_nickname", "nickname"),
        Index("ix_comments_user", "platform_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"), nullable=False)
    platform_comment_id: Mapped[str | None] = mapped_column(String(128))
    platform_user_id: Mapped[str | None] = mapped_column(String(128))
    nickname: Mapped[str | None] = mapped_column(String(128))
    is_anonymous: Mapped[bool | None] = mapped_column(Boolean)
    anonymous_label: Mapped[str | None] = mapped_column(String(32))
    comment_source: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stream_offset_ms: Mapped[int | None] = mapped_column(Integer)
    recording_offset_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CollectionGap(Base):
    __tablename__ = "collection_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeInstance(Base):
    __tablename__ = "runtime_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    stream_session_id: Mapped[int | None] = mapped_column(ForeignKey("stream_sessions.id"))
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StreamerFolder(Base):
    __tablename__ = "streamer_folders"

    normalized_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    directory_name: Mapped[str] = mapped_column(String(128))
    windows_key: Mapped[str] = mapped_column(String(128), unique=True)
    owner_token: Mapped[str] = mapped_column(String(32))


class DeletionOperation(Base):
    __tablename__ = "deletion_operations"
    __table_args__ = (Index("ix_deletion_operations_target", "target_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(24), default="stream_session")
    target_id: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    target_paths: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
