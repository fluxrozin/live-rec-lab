import logging
import re

from fastapi.testclient import TestClient

from live_rec_lab.app import create_app
from live_rec_lab.db.queries import load_settings, save_settings
from live_rec_lab.db.session import session_scope
from live_rec_lab.ops.logging_config import reset_logging
from live_rec_lab.recording.manager import recording_engines


def _field(html, name):
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    return match.group(1) if match else ""


def _settings_form(client, **changes):
    html = client.get("/settings").text
    return {
        "csrf_token": _field(html, "csrf_token"),
        "revision": _field(html, "revision"),
        "monitor_per_minute_tiktok": "10",
        "monitor_per_minute_twitcasting": "10",
        "max_parallel_recordings": "3",
        "recording_root": "",
        "min_free_bytes": "0",
        "retention_days": "0",
        "recording_quality": "best",
        "comments_enabled": "true",
        "srt_display_seconds": "4",
        "timezone_name": "Asia/Tokyo",
        "font": "windows",
        "web_port": "0",
        "log_level": "INFO",
        **changes,
    }


def test_settings_validation_retains_edits_without_applying_them(client):
    form = _settings_form(client, monitor_per_minute_tiktok="12.5", srt_display_seconds="7.5", font="noto")
    response = client.post("/settings", data=form)
    assert response.status_code == 400
    assert _field(response.text, "monitor_per_minute_tiktok") == "12.5"
    assert _field(response.text, "srt_display_seconds") == "7.5"
    assert 'id="monitor_per_minute_tiktok-error"' in response.text
    assert 'data-font="windows"' in response.text
    with session_scope() as db:
        assert load_settings(db)["srt_display_seconds"] == 4.0
        assert load_settings(db)["font"] == "windows"


def test_stale_settings_form_reports_conflict_and_keeps_my_changes(client):
    form = _settings_form(client, monitor_per_minute_tiktok="2.5")
    with session_scope() as db:
        save_settings(db, {"monitor_per_minute_tiktok": 3.5})
    response = client.post("/settings", data=form)
    assert response.status_code == 409
    assert _field(response.text, "monitor_per_minute_tiktok") == "2.5"
    with session_scope() as db:
        assert load_settings(db)["monitor_per_minute_tiktok"] == 3.5


def test_saved_log_level_applies_to_application_logs_and_survives_restart(paths):
    with TestClient(create_app(), base_url="http://127.0.0.1") as client:
        form = _settings_form(client, log_level="ERROR")
        assert client.post("/settings", data=form).status_code == 200
        logger = logging.getLogger("live_rec_lab.settings_test")
        logger.warning("settings test warning")
        logger.error("settings test error")
        logs = (paths["state"] / "logs" / "app.jsonl").read_text()
        assert "settings test warning" not in logs
        assert "settings test error" in logs
    reset_logging()
    with TestClient(create_app(), base_url="http://127.0.0.1"):
        assert not logger.isEnabledFor(logging.WARNING)


def test_port_setting_reports_pending_restart_and_saved_font_applies(client, monkeypatch):
    monkeypatch.setenv("LIVE_REC_LAB_BOUND_PORT", "8900")
    response = client.post("/settings", data=_settings_form(client, web_port="8912", font="noto"))
    assert response.status_code == 200
    assert 'data-font="noto"' in response.text
    assert "保存済み・再起動後に反映" in response.text
    assert "8900" in response.text


def test_settings_reports_when_twitcasting_monitoring_is_blocked(client):
    client.app.state.coordinator.twitcasting.gate.note(5)
    page = client.get("/settings")
    assert "API残枠 5" in page.text
    assert "監視を一時停止中" in page.text


def test_comment_collection_and_tiktok_engines_use_saved_settings(client):
    response = client.post(
        "/settings",
        data=_settings_form(client, comments_enabled="false", tiktok_engine_first="yt-dlp"),
    )
    assert response.status_code == 200
    with session_scope() as db:
        settings = load_settings(db)
    assert settings["comments_enabled"] is False
    assert recording_engines("tiktok", None, settings["tiktok_engine_first"]) == ["yt-dlp", "streamlink"]


def test_storage_limit_is_saved_as_bytes(client):
    form = _settings_form(client, max_recording_bytes="1024")
    response = client.post("/settings", data=form)
    assert response.status_code == 200
    assert _field(response.text, "max_recording_bytes") == "1024"
    with session_scope() as db:
        assert load_settings(db)["max_recording_bytes"] == 1024


def test_storage_limit_blocks_new_recording_without_removing_existing_media(client, paths, monkeypatch):
    import asyncio

    from sqlalchemy import select

    from live_rec_lab.config import utcnow
    from live_rec_lab.db.models import RecordingJob, RecordingSegment, StreamSession, SystemEvent
    from live_rec_lab.platforms.base import Snapshot
    from live_rec_lab.services.sessions import Intent, register_streamer
    from live_rec_lab.storage import ensure_recording_root

    monkeypatch.setenv("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", "none")
    monkeypatch.setattr("live_rec_lab.services.jobs.ensure_recording_root",
                        lambda root: ensure_recording_root(root, require_windows=False))
    directory = paths["recordings"]
    directory.mkdir(exist_ok=True)
    video = directory / "saved.mp4"
    video.write_bytes(b"saved")
    with session_scope() as db:
        streamer = register_streamer(
            db, platform="tiktok", account="capacity", display_name="容量",
            monitoring=True, recording=True, comments=False,
        )
        streamer.live_status = "LIVE"
        now = utcnow()
        stream = StreamSession(streamer_id=streamer.id, platform_stream_id="limit", detected_at=now, status="LIVE")
        db.add(stream)
        db.flush()
        job = RecordingJob(stream_session_id=stream.id, status="COMPLETED")
        db.add(job)
        db.flush()
        db.add(RecordingSegment(
            recording_job_id=job.id, stream_session_id=stream.id, segment_number=1,
            started_at=now, validation_status="valid",
            file_path=str(video), file_size=len(b"saved"),
        ))
        session_id = stream.id
        save_settings(db, {"max_recording_bytes": len(b"saved"), "min_free_bytes": 0})

    coordinator = client.app.state.coordinator
    coordinator._last_live[session_id] = Snapshot("LIVE", platform_stream_id="limit")
    asyncio.run(coordinator._start_recording(Intent("start_recording", session_id, "tiktok", "capacity")))
    assert not coordinator.recorder.alive(session_id)
    assert video.read_bytes() == b"saved"
    with session_scope() as db:
        event = db.scalar(select(SystemEvent).where(SystemEvent.event_type == "storage_limit"))
        assert event.stream_session_id == session_id
