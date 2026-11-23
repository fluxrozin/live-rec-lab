"""初期スキーマ。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "streamers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("platform_user_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128)),
        sa.Column("profile_url", sa.String(300)),
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False),
        sa.Column("recording_enabled", sa.Boolean(), nullable=False),
        sa.Column("comments_enabled", sa.Boolean(), nullable=False),
        sa.Column("live_status", sa.String(16), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
        sa.Column("last_live_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "platform_user_id"),
    )
    op.create_table(
        "stream_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("streamer_id", sa.Integer(), sa.ForeignKey("streamers.id"), nullable=False),
        sa.Column("platform_stream_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200)),
        sa.Column("platform_started_at", sa.DateTime(timezone=True)),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("platform_ended_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_live_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manual_stop", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("streamer_id", "platform_stream_id"),
    )
    op.create_table(
        "recording_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stream_session_id", sa.Integer(), sa.ForeignKey("stream_sessions.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("recorder_engine", sa.String(32)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("manual_stop_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "recording_segments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("recording_job_id", sa.Integer(), sa.ForeignKey("recording_jobs.id"), nullable=False),
        sa.Column("segment_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("temp_path", sa.String(500)),
        sa.Column("file_path", sa.String(500)),
        sa.Column("file_size", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("first_media_at", sa.DateTime(timezone=True)),
        sa.Column("media_start_pts", sa.Float()),
        sa.Column("sync_offset_ms", sa.Integer()),
        sa.Column("sync_method", sa.String(16)),
        sa.Column("container_format", sa.String(16)),
        sa.Column("validation_status", sa.String(16), nullable=False),
        sa.Column("srt_path", sa.String(500)),
        sa.Column("srt_status", sa.String(16), nullable=False),
        sa.Column("srt_error", sa.String(500)),
        sa.Column("srt_generated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("recording_job_id", "segment_number"),
    )
    op.create_table(
        "comment_collection_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stream_session_id", sa.Integer(), sa.ForeignKey("stream_sessions.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.Column("stored_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("reconnect_count", sa.Integer(), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stream_session_id", sa.Integer(), sa.ForeignKey("stream_sessions.id"), nullable=False),
        sa.Column("platform_comment_id", sa.String(128)),
        sa.Column("platform_user_id", sa.String(128)),
        sa.Column("nickname", sa.String(128)),
        sa.Column("is_anonymous", sa.Boolean()),
        sa.Column("anonymous_label", sa.String(32)),
        sa.Column("comment_source", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stream_offset_ms", sa.Integer()),
        sa.Column("recording_offset_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("stream_session_id", "platform_comment_id"),
    )
    op.create_index("ix_comments_session_received", "comments", ["stream_session_id", "received_at"])
    op.create_index("ix_comments_nickname", "comments", ["nickname"])
    op.create_index("ix_comments_user", "comments", ["platform_user_id"])
    op.create_table(
        "collection_gaps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stream_session_id", sa.Integer(), sa.ForeignKey("stream_sessions.id"), nullable=False),
        sa.Column("job_type", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "runtime_instances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.UniqueConstraint("instance_id"),
    )
    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("stream_session_id", sa.Integer(), sa.ForeignKey("stream_sessions.id")),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("metadata_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_system_events_created", "system_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_system_events_created", table_name="system_events")
    op.drop_table("system_events")
    op.drop_table("runtime_instances")
    op.drop_table("system_settings")
    op.drop_table("collection_gaps")
    op.drop_index("ix_comments_user", table_name="comments")
    op.drop_index("ix_comments_nickname", table_name="comments")
    op.drop_index("ix_comments_session_received", table_name="comments")
    op.drop_table("comments")
    op.drop_table("comment_collection_jobs")
    op.drop_table("recording_segments")
    op.drop_table("recording_jobs")
    op.drop_table("stream_sessions")
    op.drop_table("streamers")
