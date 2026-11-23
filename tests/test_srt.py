from datetime import timedelta

from live_rec_lab.comments.srt import build_cues, generate_segment_srt, render_srt, resync_session
from live_rec_lab.comments.store import store_comment
from live_rec_lab.config import utcnow
from live_rec_lab.db.models import (
    Comment,
    CommentCollectionJob,
    RecordingSegment,
)
from live_rec_lab.db.session import session_scope
from live_rec_lab.platforms.base import CommentIn


def _comment(message: str, posted, index: int) -> Comment:
    return Comment(
        id=index, stream_session_id=1, message=message, nickname="視聴者",
        comment_source="test", posted_at=posted, received_at=posted, created_at=posted,
    )




def test_srt_skips_comments_before_anchor_and_keeps_emoji():
    start = utcnow()
    comments = [
        _comment("前", start - timedelta(seconds=2), 1),
        _comment("こんにちは🎉", start + timedelta(seconds=1), 2),
    ]
    cues = build_cues(comments, anchor=start, duration_ms=10_000, display_seconds=4)
    assert cues is not None
    text = render_srt(cues)
    assert len(cues) == 1 and "🎉" in text and text.startswith("1\n") and "前" not in text


def test_comments_after_segment_do_not_create_empty_srt(clip):
    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="あと", posted_at=start + timedelta(seconds=60), source="test"))
    assert generate_segment_srt(segment_id) == "skipped"
    assert not video.with_suffix(".srt").exists()


def test_srt_without_anchor_is_unsynced():
    start = utcnow()
    assert build_cues([_comment("a", start, 1)], anchor=None, duration_ms=1000, display_seconds=4) is None


def test_initial_srt_does_not_overwrite_an_unmanaged_file(clip):
    stream_id, segment_id, video, start = clip
    target = video.with_suffix(".srt")
    target.write_text("別の字幕", encoding="utf-8")
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="コメント", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "failed"
    assert target.read_text(encoding="utf-8") == "別の字幕"


def test_provisional_skip_resyncs_after_comment_write(clip):
    stream_id, segment_id, video, start = clip
    with session_scope() as db:
        job = CommentCollectionJob(stream_session_id=stream_id, status="COLLECTING")
        db.add(job)
        db.flush()
        job_id = job.id
    assert generate_segment_srt(segment_id) == "skipped"
    with session_scope(write_lock=True) as db:
        status = db.get(RecordingSegment, segment_id)
        assert status.srt_provisional and status.srt_input_version == status.srt_generated_from_version
        store_comment(db, stream_id, CommentIn(message="後着🎉", posted_at=start, source="test"))
        assert status.srt_status == "stale"
        db.get(CommentCollectionJob, job_id).status = "COMPLETED"
    resync_session(stream_id)
    with session_scope() as db:
        status = db.get(RecordingSegment, segment_id)
        assert not status.srt_provisional
        assert status.srt_status == "generated"
        assert status.srt_generated_from_version == status.srt_input_version
    assert "後着🎉" in video.with_suffix(".srt").read_text()


def test_input_changed_before_publication_keeps_last_good_srt(clip, monkeypatch):
    from live_rec_lab.comments import srt

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="以前", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "generated"
    previous = video.with_suffix(".srt").read_bytes()
    publish = srt._publish

    def changed(*args, **kwargs):
        with session_scope(write_lock=True) as db:
            store_comment(db, stream_id, CommentIn(message="新着", posted_at=start, source="test"))
        return publish(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(srt, "_publish", changed)
        assert generate_segment_srt(segment_id) == "stale"
    assert video.with_suffix(".srt").read_bytes() == previous
    resync_session(stream_id)
    assert "新着" in video.with_suffix(".srt").read_text()


def test_input_changed_after_publication_resynchronizes(clip, monkeypatch):
    from live_rec_lab.comments import srt

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="以前", posted_at=start, source="test"))
    finish = srt._complete_publication

    def changed(*args, **kwargs):
        assert video.with_suffix(".srt").exists()
        with session_scope(write_lock=True) as db:
            store_comment(db, stream_id, CommentIn(message="公開後", posted_at=start, source="test"))
        return finish(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(srt, "_complete_publication", changed)
        assert generate_segment_srt(segment_id) == "stale"
    with session_scope() as db:
        result = db.get(RecordingSegment, segment_id)
        assert result.srt_input_version > result.srt_generated_from_version
    resync_session(stream_id)
    assert "公開後" in video.with_suffix(".srt").read_text()


def test_display_duration_change_updates_the_srt_input_version(clip):
    from live_rec_lab.db.queries import save_settings

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="字幕", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "generated"
    with session_scope(write_lock=True) as db:
        save_settings(db, {"srt_display_seconds": 2})
        segment = db.get(RecordingSegment, segment_id)
        assert segment.srt_input_version > segment.srt_generated_from_version
    resync_session(stream_id)
    assert "00:00:02,000" in video.with_suffix(".srt").read_text()


def test_zero_cues_keeps_previous_srt_when_unlink_fails(clip, monkeypatch):
    from pathlib import Path

    stream_id, segment_id, video, start = clip
    with session_scope(write_lock=True) as db:
        store_comment(db, stream_id, CommentIn(message="前の字幕", posted_at=start, source="test"))
    assert generate_segment_srt(segment_id) == "generated"
    subtitle = video.with_suffix(".srt")
    prior = subtitle.read_bytes()
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == subtitle:
            raise PermissionError("注入した字幕削除エラー")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        assert generate_segment_srt(segment_id, manual_offset_ms=10_000) == "failed"
    assert subtitle.read_bytes() == prior
    with session_scope() as db:
        assert db.get(RecordingSegment, segment_id).srt_status == "resync_failed"
    resync_session(stream_id)
    assert not subtitle.exists() and video.is_file()
