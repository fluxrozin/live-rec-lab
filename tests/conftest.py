
import pytest

from live_rec_lab.db.session import reset_engine
from live_rec_lab.ops.logging_config import reset_logging


@pytest.fixture
def paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    state = tmp_path / "state"
    recordings = tmp_path / "recordings"
    monkeypatch.setenv("LIVE_REC_LAB_DATA_DIR", str(data))
    monkeypatch.setenv("LIVE_REC_LAB_STATE_DIR", str(state))
    monkeypatch.setenv("LIVE_REC_LAB_RECORDING_ROOT", str(recordings))
    monkeypatch.setenv("LIVE_REC_LAB_DISABLE_SCHEDULER", "1")
    monkeypatch.delenv("TWITCASTING_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCASTING_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SIGN_API_KEY", raising=False)
    reset_engine()
    reset_logging()
    yield {"data": data, "state": state, "recordings": recordings}
    reset_engine()
    reset_logging()


@pytest.fixture
def db(paths):
    from live_rec_lab.db.session import session_scope, upgrade_database

    upgrade_database()
    with session_scope() as session:
        yield session


@pytest.fixture
def client(paths):
    from fastapi.testclient import TestClient

    from live_rec_lab.app import create_app

    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


@pytest.fixture
def clip(paths, monkeypatch):
    from datetime import timedelta

    from live_rec_lab.config import utcnow
    from live_rec_lab.db.models import RecordingJob, RecordingSegment, Streamer, StreamSession
    from live_rec_lab.db.session import session_scope, upgrade_database
    from live_rec_lab.storage import recording_root_status

    root = paths["recordings"]
    root.mkdir()
    monkeypatch.setenv("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", "none")
    monkeypatch.setattr(
        "live_rec_lab.storage.recording_root_status",
        lambda root: recording_root_status(root, require_windows=False),
    )
    target = root / "clip.mp4"
    target.write_bytes(b"verified video fixture")
    identity = target.stat()
    start = utcnow() - timedelta(days=3)
    upgrade_database()
    with session_scope() as db:
        streamer = Streamer(platform="tiktok", platform_user_id="author")
        db.add(streamer)
        db.flush()
        stream = StreamSession(
            streamer_id=streamer.id, platform_stream_id="100", status="ENDED", detected_at=start,
            platform_started_at=start, ended_at=start + timedelta(minutes=1),
        )
        db.add(stream)
        db.flush()
        job = RecordingJob(stream_session_id=stream.id, status="COMPLETED", started_at=start)
        db.add(job)
        db.flush()
        segment = RecordingSegment(
            recording_job_id=job.id, stream_session_id=stream.id, segment_number=1,
            started_at=start, file_path=str(target), file_size=identity.st_size, file_device=identity.st_dev,
            file_inode=identity.st_ino, validation_status="valid", duration_ms=6_000,
        )
        db.add(segment)
        db.flush()
        return stream.id, segment.id, target, start

