"""セッション単位の採番、管理ファイル・字幕の版数、削除の照合。"""

import sqlalchemy as sa
from alembic import op

revision = "0002_session_integrity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(sa.text("""
        SELECT j.stream_session_id, s.segment_number
        FROM recording_segments AS s JOIN recording_jobs AS j ON j.id = s.recording_job_id
        GROUP BY j.stream_session_id, s.segment_number HAVING count(*) > 1 LIMIT 1
    """)).first()
    if duplicate:
        raise RuntimeError("旧試作DBにセッション内の重複連番があります。取得済み記録を保護するため移行を保留しました。")

    date = sa.DateTime(timezone=True)
    op.add_column("streamers", sa.Column("archived_at", date))
    for name in ("ended_at", "ending_since", "orphan_since", "comments_purged_at"):
        op.add_column("stream_sessions", sa.Column(name, date))
    for name, length in (("end_reason", 128), ("filename_disambiguator", 40), ("filename_stem", 200)):
        op.add_column("stream_sessions", sa.Column(name, sa.String(length)))
    op.add_column("stream_sessions", sa.Column("next_segment_number", sa.Integer, nullable=False, server_default="1"))
    op.add_column("stream_sessions", sa.Column("purged_comment_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("recording_jobs", sa.Column("end_reason", sa.String(128)))
    op.add_column("comment_collection_jobs", sa.Column("end_reason", sa.String(128)))
    op.add_column("system_events", sa.Column("correlation_id", sa.String(64)))

    with op.batch_alter_table("stream_sessions", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass
    with op.batch_alter_table("recording_jobs", recreate="always") as batch:
        batch.create_unique_constraint("uq_recording_job_session", ["id", "stream_session_id"])
    op.add_column("recording_segments", sa.Column("stream_session_id", sa.Integer))
    op.execute("""
        UPDATE recording_segments SET stream_session_id =
            (SELECT stream_session_id FROM recording_jobs WHERE id = recording_job_id)
    """)
    op.execute("""
        UPDATE stream_sessions SET next_segment_number = 1 +
            COALESCE((SELECT MAX(segment_number) FROM recording_segments
                      WHERE stream_session_id = stream_sessions.id), 0)
    """)
    convention = {"fk": "fk_%(column_0_name)s", "uq": "uq_%(column_0_name)s"}
    with op.batch_alter_table("recording_segments", naming_convention=convention, recreate="always") as batch:
        batch.drop_constraint("fk_recording_job_id", type_="foreignkey")
        batch.drop_constraint("uq_recording_job_id", type_="unique")
        batch.alter_column("stream_session_id", nullable=False)
        batch.alter_column("segment_number", nullable=True)
        batch.create_unique_constraint("uq_recording_segment_number", ["stream_session_id", "segment_number"])
        batch.create_foreign_key(
            "fk_recording_segment_job_session",
            "recording_jobs", ["recording_job_id", "stream_session_id"], ["id", "stream_session_id"],
        )
        for name in ("reserved_path", "srt_stage_path"):
            batch.add_column(sa.Column(name, sa.String(500)))
        for name in (
            "file_device", "file_inode", "srt_device", "srt_inode", "srt_stage_device", "srt_stage_inode",
            "srt_stage_version", "srt_generated_from_version", "srt_manual_offset_ms",
        ):
            batch.add_column(sa.Column(name, sa.Integer))
        batch.add_column(sa.Column("srt_input_version", sa.Integer, nullable=False, server_default="0"))
        batch.add_column(sa.Column("srt_provisional", sa.Boolean, nullable=False, server_default="0"))
        batch.add_column(sa.Column("files_deleted_at", date))
        batch.create_index("ix_recording_segments_job", ["recording_job_id"])

    op.create_table(
        "streamer_folders",
        sa.Column("normalized_id", sa.String(128), primary_key=True),
        sa.Column("directory_name", sa.String(128), nullable=False),
        sa.Column("windows_key", sa.String(128), nullable=False, unique=True),
        sa.Column("owner_token", sa.String(32), nullable=False),
    )
    op.create_table(
        "deletion_operations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("target_type", sa.String(24), nullable=False),
        sa.Column("target_id", sa.Integer, nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("target_paths", sa.Text, nullable=False),
        sa.Column("started_at", date, nullable=False),
        sa.Column("finished_at", date),
        sa.Column("last_error", sa.Text),
    )
    op.create_index("ix_deletion_operations_target", "deletion_operations", ["target_id", "status"])


def downgrade() -> None:
    raise RuntimeError("採番と操作台帳を保護するため、移行前に作成したバックアップを用いてください")
