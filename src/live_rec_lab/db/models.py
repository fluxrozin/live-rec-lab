"""SQLAlchemyモデル（システム設計§9、最も単純な形）。"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)
ID = BigInteger().with_variant(Integer, "sqlite")


def _pk() -> Column:
    return Column("id", ID, primary_key=True, autoincrement=True)


streamers = Table(
    "streamers", metadata, _pk(),
    Column("platform", String(16), nullable=False),
    Column("platform_user_id", String(128), nullable=False),  # 正規化済みID
    Column("display_name", String(128), nullable=False),
    Column("profile_url", Text),
    Column("monitoring_enabled", Boolean, nullable=False),
    Column("recording_enabled", Boolean, nullable=False),
    Column("comments_enabled", Boolean, nullable=False),
    Column("last_checked_at", String(32)),
    Column("last_live_at", String(32)),
    Column("sort_order", Integer),
    Column("archived_at", String(32)),
    Column("folder_name", String(160)),  # 固定した保存フォルダ名（サービス非依存）
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    UniqueConstraint("platform", "platform_user_id", name="uq_streamers_platform_user"),
    CheckConstraint("platform in ('tiktok','twitcasting')", name="platform"),
)

stream_sessions = Table(
    "stream_sessions", metadata, _pk(),
    Column("streamer_id", ID, ForeignKey("streamers.id"), nullable=False),
    Column("platform_stream_id", String(128)),
    Column("title", Text),
    Column("platform_started_at", String(32)),
    Column("detected_at", String(32), nullable=False),
    Column("platform_ended_at", String(32)),
    Column("ended_at", String(32)),
    Column("filename_datetime", String(32)),
    Column("filename_title", String(160)),
    Column("filename_disambiguator", String(64)),
    Column("end_reason", String(32)),
    Column("last_seen_live_at", String(32)),
    Column("next_segment_number", Integer, nullable=False, server_default="1"),
    Column("status", String(16), nullable=False),  # LIVE / ENDING / ENDED / ORPHANED / UNCONFIRMED
    Column("recording_root", String(1024)),  # セッションに固定した保存ルート
    Column("manual_convergence_at", String(32)),
    Column("collection_converged_at", String(32)),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    UniqueConstraint("streamer_id", "platform_stream_id", name="uq_stream_sessions_streamer_stream"),
    UniqueConstraint("id", "streamer_id", name="uq_stream_sessions_id_streamer"),
    Index("ix_stream_sessions_streamer_detected", "streamer_id", "detected_at"),
)

recording_jobs = Table(
    "recording_jobs", metadata, _pk(),
    Column("stream_session_id", ID, ForeignKey("stream_sessions.id"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("recorder_engine", String(32)),
    Column("started_at", String(32)),
    Column("finished_at", String(32)),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("retry_budget_id", Integer),
    Column("last_error", Text),
    Column("manual_stop_requested", Boolean, nullable=False, server_default="0"),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    UniqueConstraint("id", "stream_session_id", name="uq_recording_jobs_id_session"),
)

recording_segments = Table(
    "recording_segments", metadata, _pk(),
    Column("recording_job_id", ID, nullable=False),
    Column("stream_session_id", ID, nullable=False),
    Column("segment_number", Integer),
    Column("recorder_engine", String(32)),
    Column("attempt_id", String(64)),
    Column("started_at", String(32)),
    Column("finished_at", String(32)),
    Column("file_path", Text),  # セッション固定ルートからの相対パス
    Column("temp_path", Text),
    Column("file_size", BigInteger),
    Column("duration_ms", BigInteger),
    Column("first_media_at", String(32)),
    Column("media_start_pts", String(32)),
    Column("sync_offset_ms", Integer),
    Column("sync_method", String(32)),
    Column("container_format", String(32)),
    Column("video_codec", String(32)),
    Column("validation_status", String(16), nullable=False),  # pending / valid / invalid
    Column("file_identity", Text),  # 世代識別（JSON）
    Column("srt_path", Text),
    Column("srt_status", String(16), nullable=False, server_default="none"),
    Column("srt_generated_at", String(32)),
    Column("srt_error", Text),
    Column("srt_offset_ms", Integer, nullable=False, server_default="0"),
    Column("speech_srt_path", Text),
    Column("speech_status", String(16), nullable=False, server_default="none"),
    Column("speech_generated_at", String(32)),
    Column("speech_error", Text),
    Column("ass_path", Text),
    Column("ass_status", String(16), nullable=False, server_default="none"),
    Column("ass_generated_at", String(32)),
    Column("ass_error", Text),
    Column("created_at", String(32), nullable=False),
    ForeignKeyConstraint(
        ["recording_job_id", "stream_session_id"], ["recording_jobs.id", "recording_jobs.stream_session_id"],
        name="fk_segments_job_session",
    ),
    UniqueConstraint("stream_session_id", "segment_number", name="uq_segments_session_number"),
)

comment_collection_jobs = Table(
    "comment_collection_jobs", metadata, _pk(),
    Column("stream_session_id", ID, ForeignKey("stream_sessions.id"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", String(32)),
    Column("finished_at", String(32)),
    Column("received_count", Integer, nullable=False, server_default="0"),
    Column("stored_count", Integer, nullable=False, server_default="0"),
    Column("duplicate_count", Integer, nullable=False, server_default="0"),
    Column("discarded_count", Integer, nullable=False, server_default="0"),
    Column("save_failed_count", Integer, nullable=False, server_default="0"),
    Column("last_received_at", String(32)),
    Column("last_durable_at", String(32)),
    Column("counts_complete", Boolean, nullable=False, server_default="1"),
    Column("error_count", Integer, nullable=False, server_default="0"),
    Column("reconnect_count", Integer, nullable=False, server_default="0"),
    Column("retry_budget_id", Integer),
    Column("last_error", Text),
    Column("connection_generation", Integer, nullable=False, server_default="0"),
    Column("verified_platform_stream_id", String(128)),
    Column("verified_boundary", Text),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
)

comments = Table(
    "comments", metadata, _pk(),
    Column("stream_session_id", ID, ForeignKey("stream_sessions.id"), nullable=False),
    Column("platform_comment_id", String(128)),
    Column("platform_user_id", String(128)),
    Column("nickname", Text),
    Column("is_anonymous", Boolean),
    Column("anonymous_label", String(32)),
    Column("comment_source", String(16), nullable=False),
    Column("message", Text, nullable=False),
    Column("posted_at", String(32)),
    Column("received_at", String(32), nullable=False),
    Column("effective_at", String(32), nullable=False),  # 実効時刻（posted_at優先、なければreceived_at）
    Column("stream_offset_ms", BigInteger),
    Column("recording_offset_ms", BigInteger),
    Column("created_at", String(32), nullable=False),
    Index("ix_comments_session_effective", "stream_session_id", "effective_at", "id"),
    Index("ix_comments_effective", "effective_at", "id"),
    Index("uq_comments_session_platform_id", "stream_session_id", "platform_comment_id", unique=True,
          sqlite_where=Column("platform_comment_id").isnot(None)),
)

collection_gaps = Table(
    "collection_gaps", metadata, _pk(),
    Column("stream_session_id", ID, ForeignKey("stream_sessions.id"), nullable=False),
    Column("job_type", String(16), nullable=False),
    Column("started_at", String(32)),
    Column("ended_at", String(32)),
    Column("reason", Text, nullable=False),
    Column("recovery_key", String(128)),
    Column("start_basis", String(16)),
    Column("end_basis", String(16)),
    Column("gap_status", String(8), nullable=False),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("stream_session_id", "recovery_key", name="uq_gaps_session_recovery"),
)

system_settings = Table(
    "system_settings", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),  # JSON
    Column("updated_at", String(32), nullable=False),
)

runtime_instances = Table(
    "runtime_instances", metadata, _pk(),
    Column("instance_id", String(64), nullable=False, unique=True),
    Column("started_at", String(32), nullable=False),
    Column("stopped_at", String(32)),
    Column("last_heartbeat_at", String(32)),
    Column("process_id", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("code_revision", String(64)),
)

system_events = Table(
    "system_events", metadata, _pk(),
    Column("event_type", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("stream_session_id", ID, ForeignKey("stream_sessions.id", ondelete="SET NULL")),
    Column("message", Text, nullable=False),
    Column("metadata", Text),
    Column("created_at", String(32), nullable=False),
    Index("ix_system_events_created", "created_at", "id"),
)

deletion_operations = Table(
    "deletion_operations", metadata, _pk(),
    Column("target_type", String(16), nullable=False),
    Column("target_id", ID, nullable=False),
    Column("trigger", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("target_paths", Text),
    Column("target_entries", Text),
    Column("started_at", String(32), nullable=False),
    Column("finished_at", String(32)),
    Column("last_error", Text),
)

retry_budgets = Table(
    "retry_budgets", metadata, _pk(),
    Column("stream_session_id", ID, ForeignKey("stream_sessions.id"), nullable=False),
    Column("kind", String(16), nullable=False),  # recording / comments
    Column("generation", Integer, nullable=False),
    Column("max_extra", Integer, nullable=False),  # 開始時設定の追加試行上限（固定）
    Column("used_extra", Integer, nullable=False, server_default="0"),
    Column("initial_reserved", Boolean, nullable=False, server_default="0"),
    Column("last_attempt_id", String(64)),
    Column("suppression", String(32)),  # manual_stop / exhausted / limited / ...
    Column("next_attempt_at", String(32)),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("stream_session_id", "kind", "generation", name="uq_retry_budgets_session_kind_gen"),
)
