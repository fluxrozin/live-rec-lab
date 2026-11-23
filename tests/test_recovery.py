import sqlite3
from datetime import timedelta
from pathlib import Path

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import RecordingJob, RecordingSegment, StreamSession
from live_rec_lab.ops.backup import backup_database
from live_rec_lab.ops.recovery import recover
from live_rec_lab.recording.segments import Probe
from live_rec_lab.services.sessions import register_streamer
from live_rec_lab.storage import publish_exclusive


def test_backup_is_consistent(paths, db):
    register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=False,
        comments=False,
    )
    db.commit()
    dest = paths["state"] / "backup.sqlite3"
    backup_database(dest)
    copied = sqlite3.connect(dest)
    try:
        count = copied.execute("select count(*) from streamers").fetchone()[0]
    finally:
        copied.close()
    assert count == 1


def test_recovery_does_not_mark_unverified_complete(db, tmp_path):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=True,
        comments=False,
    )
    now = utcnow()
    session = StreamSession(
        streamer_id=streamer.id,
        platform_stream_id="room",
        detected_at=now - timedelta(minutes=5),
        status="LIVE",
        manual_stop=False,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    job = RecordingJob(
        stream_session_id=session.id,
        status="RECORDING",
        retry_count=0,
        manual_stop_requested=False,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    temp = tmp_path / "partial.part.ts"
    temp.write_bytes(b"not media")
    db.add(
        RecordingSegment(
            recording_job_id=job.id,
            stream_session_id=session.id,
            segment_number=1,
            started_at=now,
            temp_path=str(temp),
            reserved_path=str(tmp_path / "partial.mp4"),
            validation_status="pending",
            srt_status="not_generated",
            created_at=now,
        )
    )
    db.flush()
    recover(db, root=tmp_path)
    assert job.status == "FAILED"
    assert job.status != "COMPLETED"


def test_cancelled_pending_segment_is_finalized_on_startup(db, tmp_path):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=True,
        comments=False,
    )
    now = utcnow()
    session = StreamSession(
        streamer_id=streamer.id,
        platform_stream_id="room",
        detected_at=now,
        status="ENDED",
        manual_stop=False,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    job = RecordingJob(
        stream_session_id=session.id,
        status="CANCELLED",
        retry_count=1,
        manual_stop_requested=False,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    temp = tmp_path / "kept.part.ts"
    temp.write_bytes(b"not media but present")
    segment = RecordingSegment(
        recording_job_id=job.id,
        stream_session_id=session.id,
        segment_number=1,
        started_at=now,
        temp_path=str(temp),
        reserved_path=str(tmp_path / "kept.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    db.add(segment)
    db.flush()
    recover(db, root=tmp_path)
    db.refresh(segment)
    assert segment.validation_status == "invalid"
    assert segment.validation_status != "pending"
    assert job.status == "FAILED"


def test_manual_cancel_of_an_empty_attempt_stays_cancelled(db, tmp_path):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="beta",
        display_name="B",
        monitoring=True,
        recording=True,
        comments=False,
    )
    now = utcnow()
    session = StreamSession(
        streamer_id=streamer.id,
        platform_stream_id="room",
        detected_at=now,
        status="ENDED",
        manual_stop=True,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    job = RecordingJob(
        stream_session_id=session.id,
        status="CANCELLED",
        retry_count=0,
        manual_stop_requested=True,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    temp = tmp_path / "empty.part.ts"
    temp.write_bytes(b"")
    segment = RecordingSegment(
        recording_job_id=job.id,
        stream_session_id=session.id,
        started_at=now,
        temp_path=str(temp),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    db.add(segment)
    db.flush()
    segment_id = segment.id
    recover(db, root=tmp_path)
    db.expire_all()
    assert db.get(RecordingSegment, segment_id) is None
    assert job.status == "CANCELLED"


def test_recovery_recognizes_exclusive_publish_before_database_commit(db, tmp_path, monkeypatch):
    streamer = register_streamer(db, platform="tiktok", account="recovery", display_name="Recovery",
                                 monitoring=True, recording=True, comments=False)
    now = utcnow()
    broadcast = StreamSession(streamer_id=streamer.id, platform_stream_id="room", detected_at=now, status="LIVE",
                              next_segment_number=2)
    db.add(broadcast)
    db.flush()
    job = RecordingJob(stream_session_id=broadcast.id, status="FINALIZING", started_at=now)
    db.add(job)
    db.flush()
    temporary = tmp_path / ".attempt.ts"
    temporary.write_bytes(b"captured bytes")
    staged = tmp_path / ".attempt.ts.remux.mp4"
    staged.write_bytes(b"converted bytes")
    final = tmp_path / "movie_s001.mp4"
    db.add(RecordingSegment(recording_job_id=job.id, stream_session_id=broadcast.id, segment_number=1,
                            started_at=now, temp_path=str(temporary), reserved_path=str(final)))
    db.commit()
    publish_exclusive(staged, final)
    monkeypatch.setattr("live_rec_lab.recording.segments.probe_media",
                        lambda _path: Probe(400, "mp4", 0.0, "h264", audio_codec="aac"))

    assert recover(db, root=tmp_path) == {broadcast.id}

    segment = db.query(RecordingSegment).one()
    assert segment.validation_status == "valid"
    assert Path(segment.file_path) == final
    assert final.read_bytes() == b"converted bytes"
    assert segment.temp_path is None
    assert (segment.segment_number, broadcast.next_segment_number) == (1, 2)
