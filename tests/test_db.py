from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from live_rec_lab.db.models import RecordingJob, RecordingSegment, Streamer, StreamSession


def test_session_scoped_segment_constraint_and_composite_foreign_key(db):
    now = datetime.now(UTC)
    streamer = Streamer(platform="tiktok", platform_user_id="viewer")
    db.add(streamer)
    db.flush()
    streams = [
        StreamSession(streamer_id=streamer.id, platform_stream_id=str(number), detected_at=now, status="LIVE")
        for number in (1, 2)
    ]
    db.add_all(streams)
    db.flush()
    jobs = [RecordingJob(stream_session_id=streams[0].id, status="PENDING") for _ in range(2)]
    db.add_all(jobs)
    db.flush()
    assert db.scalar(text("PRAGMA foreign_keys")) == 1

    def segment(job, session_id, number):
        return RecordingSegment(
            recording_job_id=job.id, stream_session_id=session_id, segment_number=number, started_at=now,
        )

    db.add(segment(jobs[0], streams[0].id, 1))
    db.flush()
    for candidate in (segment(jobs[1], streams[0].id, 1), segment(jobs[0], streams[1].id, 2)):
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(candidate)
            db.flush()

    db.add_all([segment(jobs[0], streams[0].id, None), segment(jobs[1], streams[0].id, None)])
    db.flush()
    assert len(list(db.scalars(select(RecordingSegment)))) == 3


def test_upgrade_preserves_legacy_data_and_continues_reserved_numbers(paths):
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from live_rec_lab.config import load_paths
    from live_rec_lab.db.session import session_scope, upgrade_database

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    root = Path(__file__).resolve().parents[1]
    config.set_main_option("script_location", str(root / "alembic"))
    app = load_paths()
    app.ensure()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{app.db_path}")
    command.upgrade(config, "0001_initial")
    stamp = "2026-11-24 03:00:00.000000"
    with closing(sqlite3.connect(app.db_path)) as legacy:
        legacy.execute(
            "INSERT INTO streamers (id, platform, platform_user_id, monitoring_enabled, recording_enabled, "
            "comments_enabled, live_status, created_at, updated_at) "
            "VALUES (1, 'tiktok', 'user', 1, 1, 1, 'LIVE', ?, ?)",
            (stamp, stamp),
        )
        legacy.execute(
            "INSERT INTO stream_sessions (id, streamer_id, platform_stream_id, detected_at, status, "
            "manual_stop, created_at, updated_at) VALUES (1, 1, 'room', ?, 'LIVE', 0, ?, ?)",
            (stamp, stamp, stamp),
        )
        legacy.execute(
            "INSERT INTO recording_jobs (id, stream_session_id, status, retry_count, manual_stop_requested, "
            "created_at, updated_at) VALUES (1, 1, 'COMPLETED', 0, 0, ?, ?)",
            (stamp, stamp),
        )
        legacy.execute(
            "INSERT INTO recording_segments (id, recording_job_id, segment_number, started_at, file_path, "
            "validation_status, srt_status, created_at) VALUES (1, 1, 7, ?, '/old/clip.mp4', 'valid', 'skipped', ?)",
            (stamp, stamp),
        )
        legacy.commit()

    upgrade_database()
    with session_scope() as db:
        stream = db.get(StreamSession, 1)
        segment = db.get(RecordingSegment, 1)
        assert stream.next_segment_number == 8
        assert segment.stream_session_id == 1
        assert segment.segment_number == 7 and segment.file_path == "/old/clip.mp4"
        assert db.execute(text("PRAGMA foreign_key_check")).first() is None


def test_comment_batch_retries_after_real_sqlite_write_lock(paths):
    import asyncio
    import sqlite3
    from contextlib import closing

    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from live_rec_lab.comments.store import save_comments
    from live_rec_lab.config import load_paths, utcnow
    from live_rec_lab.db.models import Comment, CommentCollectionJob
    from live_rec_lab.db.session import get_engine, session_scope, upgrade_database
    from live_rec_lab.platforms.base import CommentIn
    from live_rec_lab.services import jobs

    upgrade_database()
    now = utcnow()
    with session_scope() as db:
        streamer = Streamer(platform="tiktok", platform_user_id="lock")
        db.add(streamer)
        db.flush()
        stream = StreamSession(streamer_id=streamer.id, platform_stream_id="lock", detected_at=now, status="LIVE")
        db.add(stream)
        db.flush()
        job = CommentCollectionJob(stream_session_id=stream.id, status="COLLECTING")
        db.add(job)
        db.flush()

    def set_timeout(connection, *_):
        connection.execute("PRAGMA busy_timeout=20")

    failures = []
    engine = get_engine()
    event.listen(engine, "checkout", set_timeout)
    try:
        with closing(sqlite3.connect(load_paths().db_path, check_same_thread=False)) as holder:
            holder.execute("BEGIN IMMEDIATE")

            def save_and_release(*args):
                try:
                    return save_comments(*args)
                except OperationalError as error:
                    failures.append(str(error.orig))
                    holder.rollback()
                    raise

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(jobs, "save_comments", save_and_release)
                asyncio.run(jobs.Coordinator._persist_batch(
                    stream.id, job.id, [CommentIn(message="ロック解除後", source="test")],
                ))
    finally:
        event.remove(engine, "checkout", set_timeout)
    assert failures == ["database is locked"]
    with session_scope() as db:
        assert db.scalar(select(Comment.message)) == "ロック解除後"
        assert db.get(CommentCollectionJob, job.id).stored_count == 1
