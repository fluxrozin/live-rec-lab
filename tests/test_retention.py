from pathlib import Path

import pytest

from live_rec_lab.comments.srt import generate_segment_srt
from live_rec_lab.comments.store import store_comment
from live_rec_lab.db.models import Comment, RecordingSegment, StreamSession
from live_rec_lab.db.session import session_scope
from live_rec_lab.platforms.base import CommentIn


def test_expiry_preserves_database_until_managed_subtitle_is_removed(clip, monkeypatch):
    from live_rec_lab.services.retention import delete_session, resume_operations

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="期限あり", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "generated"
    subtitle = video.with_suffix(".srt")
    unlink = Path.unlink

    def deny(path, *args, **kwargs):
        if path == subtitle:
            raise PermissionError("注入したアクセス拒否")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny)
    result = delete_session(stream_id, "comment_expiry")
    assert result.status == "partial" and subtitle.is_file()
    with session_scope(write_lock=True) as db:
        assert db.query(Comment).count() == 1
        with pytest.raises(ValueError, match="削除"):
            store_comment(db, stream_id, CommentIn(message="遅延", posted_at=start, source="test"))
    monkeypatch.setattr(Path, "unlink", unlink)
    assert resume_operations()
    assert video.is_file() and not subtitle.exists()
    with session_scope() as db:
        assert db.query(Comment).count() == 0
        assert db.get(StreamSession, stream_id).purged_comment_count == 1
        assert db.get(RecordingSegment, segment_id).srt_status == "expired"
    assert generate_segment_srt(segment_id) == "failed"


@pytest.mark.parametrize("mode", ["files", "files_and_history"])
def test_session_delete_modes_keep_the_expected_records(clip, mode):
    from live_rec_lab.services.retention import delete_session

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="記録", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "generated"
    assert delete_session(stream_id, mode).status == "completed"
    assert not video.exists() and not video.with_suffix(".srt").exists()
    with session_scope() as db:
        if mode == "files":
            assert db.get(RecordingSegment, segment_id).files_deleted_at is not None
            assert db.query(Comment).count() == 1
        else:
            assert db.get(StreamSession, stream_id) is None
            assert db.query(Comment).count() == 0


def test_old_snapshot_restores_expired_comments_until_startup_reconciles(clip, paths, tmp_path):
    import sqlite3
    from contextlib import closing

    from live_rec_lab.db.queries import save_settings
    from live_rec_lab.db.session import reset_engine
    from live_rec_lab.ops.backup import backup_database
    from live_rec_lab.services.jobs import Coordinator
    from live_rec_lab.services.retention import run_retention

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="古いバックアップの原文", posted_at=start, source="test"))
        settings = save_settings(db, {"comment_retention_days": 1})
    assert generate_segment_srt(segment_id) == "generated"

    snapshot = backup_database(tmp_path / "old.sqlite3")
    assert run_retention(settings)
    assert not video.with_suffix(".srt").exists()
    reset_engine()
    with (
        closing(sqlite3.connect(snapshot)) as source,
        closing(sqlite3.connect(paths["data"] / "app.sqlite3")) as restored,
    ):
        source.backup(restored)
        assert restored.execute("SELECT message FROM comments").fetchone() == ("古いバックアップの原文",)

    coordinator = Coordinator()
    coordinator.startup()
    assert coordinator.retention_ready
    with session_scope() as db:
        assert db.query(Comment).count() == 0
        assert db.get(StreamSession, stream_id).comments_purged_at
        assert db.get(RecordingSegment, segment_id).srt_status == "expired"
    assert video.exists() and not video.with_suffix(".srt").exists()
    with closing(sqlite3.connect(snapshot)) as source:
        assert source.execute("SELECT message FROM comments").fetchone() == ("古いバックアップの原文",)


def test_video_retention_only_removes_confirmed_managed_files(clip):
    from live_rec_lab.db.queries import save_settings
    from live_rec_lab.services.retention import run_retention

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="動画期限後も保持", posted_at=start, source="test"))
        settings = save_settings(db, {"retention_days": 1, "auto_delete_recordings": False})
    assert generate_segment_srt(segment_id) == "generated"
    assert run_retention(settings)
    assert video.is_file()
    with session_scope(write_lock=True) as db:
        settings = save_settings(db, {"auto_delete_recordings": True})
    assert run_retention(settings)
    assert not video.exists() and not video.with_suffix(".srt").exists()
    with session_scope() as db:
        assert db.query(Comment).count() == 1
        assert db.get(StreamSession, stream_id).comments_purged_at is None
        assert db.get(RecordingSegment, segment_id).files_deleted_at


def test_active_comment_queue_postpones_expiry(clip):
    from live_rec_lab.db.models import CommentCollectionJob
    from live_rec_lab.db.queries import save_settings
    from live_rec_lab.services.retention import run_retention

    stream_id, _, video, start = clip
    with session_scope(write_lock=True) as db:
        job = CommentCollectionJob(stream_session_id=stream_id, status="COLLECTING")
        db.add(job)
        db.flush()
        store_comment(db, stream_id, CommentIn(message="書込中の記録", posted_at=start, source="test"))
        settings = save_settings(db, {"comment_retention_days": 1})
    assert not run_retention(settings)
    with session_scope() as db:
        assert db.query(Comment).count() == 1
        db.get(CommentCollectionJob, job.id).status = "COMPLETED"
    assert run_retention(settings)
    assert video.is_file()
    with session_scope() as db:
        assert db.query(Comment).count() == 0


def test_operation_resumes_after_file_removed_before_database_commit(clip, monkeypatch):
    from live_rec_lab.services import retention

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="復旧対象", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "generated"

    def interrupted(db, operation):
        assert not video.with_suffix(".srt").exists()
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(retention, "_finish", interrupted)
        with pytest.raises(KeyboardInterrupt):
            retention.delete_session(stream_id, "comment_expiry")
    with session_scope() as db:
        assert db.query(Comment).count() == 1
        assert retention.deletion_pending(db, stream_id)
    assert retention.resume_operations()
    with session_scope() as db:
        assert db.query(Comment).count() == 0
        assert db.get(RecordingSegment, segment_id).srt_status == "expired"
        assert not retention.deletion_pending(db, stream_id)
    assert video.is_file()
