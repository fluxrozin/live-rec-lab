import re

from live_rec_lab.comments.store import store_comment
from live_rec_lab.config import utcnow
from live_rec_lab.db.models import CommentCollectionJob, RecordingJob, RecordingSegment, StreamSession
from live_rec_lab.db.session import session_scope
from live_rec_lab.platforms.base import CommentIn, Snapshot
from live_rec_lab.services.sessions import apply_snapshot, register_streamer


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_recording_comment_count_refreshes_until_the_session_ends(client):
    with session_scope() as db:
        streamer = register_streamer(
            db,
            platform="tiktok",
            account="alpha",
            display_name="アルファ",
            monitoring=True,
            recording=False,
            comments=True,
        )
        intents = apply_snapshot(
            db, streamer, Snapshot("LIVE", platform_stream_id="room"), utcnow(),
            grace=30, recorder_alive=lambda _session_id: False,
        )
        session_id = next(item.session_id for item in intents if item.kind == "start_comments")
        job = CommentCollectionJob(stream_session_id=session_id, status="COLLECTING")
        db.add(job)
        db.flush()
        job_id = job.id

    detail_url = f"/recordings/{session_id}"
    count_url = f"{detail_url}?comment_count_only=true"
    for url in ("/recordings?platform=tiktok&q=alpha", detail_url):
        page = client.get(url)
        assert f'hx-get="{count_url}"' in page.text
        assert 'hx-trigger="every 5s"' in page.text
        assert '>0</span>' in page.text

    with session_scope() as db:
        store_comment(db, session_id, CommentIn(message="新着コメント", source="test"))
    fragment = client.get(count_url)
    assert fragment.status_code == 200
    assert fragment.headers["cache-control"] == "no-store"
    assert '>1</span>' in fragment.text
    assert 'hx-trigger="every 5s"' in fragment.text
    assert "<form" not in fragment.text

    with session_scope() as db:
        db.get(StreamSession, session_id).status = "ENDED"
    assert 'hx-trigger="every 5s"' in client.get(count_url).text
    with session_scope() as db:
        db.get(CommentCollectionJob, job_id).status = "COMPLETED"
    assert client.get(count_url).text.strip() == "<span>1</span>"


def test_pages_and_streamer_roundtrip(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "ダッシュボード" in home.text
    assert "稼働中の配信はありません。" in home.text
    page = client.get("/streamers")
    token = _csrf(page.text)
    created = client.post(
        "/streamers",
        data={
            "csrf_token": token,
            "platform": "tiktok",
            "account": "@alpha",
            "display_name": "アルファ",
            "monitoring_enabled": "1",
            "recording_enabled": "1",
            "comments_enabled": "1",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    again = client.get("/streamers")
    assert "アルファ" in again.text
    assert "未確認" in client.get("/").text
    duplicate = client.post(
        "/streamers",
        data={
            "csrf_token": _csrf(again.text),
            "platform": "tiktok",
            "account": "alpha",
            "display_name": "別名",
            "monitoring_enabled": "1",
        },
    )
    assert duplicate.status_code == 400
    denied = client.post("/streamers/1/delete")
    assert denied.status_code == 422 or denied.status_code == 403


def test_settings_reject_tiktok_rate(client):
    page = client.get("/settings")
    token = _csrf(page.text)
    response = client.post(
        "/settings",
        data={
            "csrf_token": token,
            "monitor_per_minute_tiktok": "11",
            "monitor_per_minute_twitcasting": "10",
            "max_parallel_recordings": "3",
            "recording_root": "",
            "min_free_bytes": "0",
            "retention_days": "0",
            "timezone_name": "Asia/Tokyo",
            "font": "windows",
            "srt_display_seconds": "4",
            "web_port": "0",
            "recording_quality": "best",
        },
    )
    assert response.status_code == 400


def test_recording_detail_lists_numbers_in_order(client):
    with session_scope() as db:
        streamer = register_streamer(
            db,
            platform="tiktok",
            account="alpha",
            display_name="アルファ",
            monitoring=False,
            recording=False,
            comments=False,
        )
        now = utcnow()
        stream = StreamSession(
            streamer_id=streamer.id,
            platform_stream_id="room",
            detected_at=now,
            status="ENDED",
            manual_stop=False,
            created_at=now,
            updated_at=now,
        )
        db.add(stream)
        db.flush()
        later = RecordingJob(
            stream_session_id=stream.id,
            status="COMPLETED",
            retry_count=0,
            manual_stop_requested=False,
            created_at=now,
            updated_at=now,
        )
        earlier = RecordingJob(
            stream_session_id=stream.id,
            status="COMPLETED",
            retry_count=0,
            manual_stop_requested=False,
            created_at=now,
            updated_at=now,
        )
        db.add(later)
        db.add(earlier)
        db.flush()
        db.add(
            RecordingSegment(
                recording_job_id=later.id,
                stream_session_id=stream.id,
                segment_number=2,
                started_at=now,
                validation_status="valid",
                srt_status="not_generated",
                created_at=now,
            )
        )
        db.add(
            RecordingSegment(
                recording_job_id=earlier.id,
                stream_session_id=stream.id,
                segment_number=1,
                started_at=now,
                validation_status="valid",
                srt_status="not_generated",
                created_at=now,
            )
        )
        session_id = stream.id
    page = client.get(f"/recordings/{session_id}")
    assert page.status_code == 200
    assert re.findall(r'class="num">(\d+)', page.text) == ["1", "2"]
    assert "完了" in page.text


def test_api_requires_csrf_and_does_not_serve_local_media(client, paths, monkeypatch):
    page = client.get("/streamers")
    token = _csrf(page.text)
    client.cookies.set("lrl_csrf", token)
    missing = client.post("/api/v1/streamers", json={"platform": "tiktok", "account": "beta"})
    assert missing.status_code == 403
    created = client.post(
        "/api/v1/streamers",
        json={"platform": "tiktok", "account": "beta", "monitoring_enabled": False,
              "recording_enabled": False, "comments_enabled": False},
        headers={"X-CSRF-Token": token},
    )
    assert created.status_code == 201
    paths["recordings"].mkdir(exist_ok=True)
    video = paths["recordings"] / "recording.mp4"
    subtitle = video.with_suffix(".srt")
    video.write_bytes(b"test video")
    subtitle.write_text("old subtitle", encoding="utf-8")
    from live_rec_lab.storage import recording_root_status

    monkeypatch.setenv("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", "none")
    monkeypatch.setattr("live_rec_lab.storage.recording_root_status",
                        lambda root: recording_root_status(root, require_windows=False))
    video_stat = video.stat()
    subtitle_stat = subtitle.stat()
    with session_scope() as db:
        streamer = register_streamer(
            db,
            platform="twitcasting",
            account="caster",
            display_name="c",
            monitoring=False,
            recording=False,
            comments=False,
        )
        now = utcnow()
        session = StreamSession(
            streamer_id=streamer.id,
            platform_stream_id="m1",
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
            status="COMPLETED",
            retry_count=0,
            manual_stop_requested=False,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        segment = RecordingSegment(
            recording_job_id=job.id,
            stream_session_id=session.id,
            segment_number=1,
            started_at=now,
            file_path=str(video),
            file_size=video_stat.st_size, file_inode=video_stat.st_ino, file_device=video_stat.st_dev,
            duration_ms=5000,
            validation_status="valid",
            srt_path=str(subtitle),
            srt_inode=subtitle_stat.st_ino, srt_device=subtitle_stat.st_dev,
            srt_status="generated",
            created_at=now,
        )
        db.add(segment)
        store_comment(db, session.id, CommentIn(message="字幕に残るコメント", source="test"))
        db.flush()
        session_id = session.id
        segment_id = segment.id

    detail = client.get(f"/recordings/{session_id}")
    assert detail.status_code == 200
    for resource in ("file", "srt"):
        url = f"/api/v1/segments/{segment_id}/{resource}"
        assert url not in detail.text
        assert client.get(url).status_code in (404, 405)
    regenerated = client.post(
        f"/api/v1/segments/{segment_id}/srt",
        headers={"X-CSRF-Token": token},
    )
    assert regenerated.json()["srt_status"] == "generated"
    assert "字幕に残るコメント" in subtitle.read_text(encoding="utf-8")
    assert video.read_bytes() == b"test video"
    assert "字幕に残るコメント" in client.get(f"/comments?session_id={session_id}").text
