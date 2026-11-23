import json
import re
from datetime import UTC, datetime

from live_rec_lab.db.models import SystemEvent
from live_rec_lab.db.session import session_scope


def test_primary_navigation_uses_spec_order_and_labels(client):
    expected = [
        ("/", "ダッシュボード"), ("/streamers", "配信者一覧"),
        ("/recordings", "録画履歴"), ("/comments", "コメント"),
        ("/system", "システム状態"), ("/logs", "ログ"), ("/settings", "設定"),
    ]
    for url, title in expected:
        response = client.get(url)
        assert response.status_code == 200
        nav = response.text.split("<nav", 1)[1].split("</nav>", 1)[0]
        assert re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', nav) == expected
        assert "運用・データ" in nav and "管理" in nav
        assert f"<h1>{title}</h1>" in response.text


def test_logs_filter_display_and_link_only_recorded_targets(client, paths):
    moment = datetime(2026, 11, 23, 0, 5, tzinfo=UTC)
    with session_scope() as db:
        db.add(SystemEvent(
            event_type="recording_failed", severity="error", message="診断の要約",
            metadata_json=json.dumps({"correlation_id": "trace-1", "component": "recorder"}),
            created_at=moment,
        ))
    folder = paths["state"] / "logs"
    diagnostic = {
        "timestamp": moment.isoformat(), "level": "ERROR", "component": "recorder",
        "message": "詳細 <script>unsafe</script>\n二行目", "correlation_id": "trace-1",
        "exception": "トレース", "stream_session_id": 123,
    }
    with (folder / "app.jsonl").open("a", encoding="utf-8") as logfile:
        logfile.write(json.dumps(diagnostic) + "\n")
        logfile.write(json.dumps({**diagnostic, "level": "INFO", "message": "別の記録"}) + "\n")
    system = client.get("/system")
    assert "診断の要約" in system.text
    assert "correlation_id=trace-1" in system.text
    assert "トレース" not in system.text
    page = client.get("/logs", params={"level": "ERROR", "q": "二行目"})
    assert page.status_code == 200
    assert "二行目" in page.text and "&lt;script&gt;" in page.text and "トレース" in page.text
    assert "別の記録" not in page.text
    assert "/recordings/123" not in page.text
    assert "条件に一致するログはありません" in client.get("/logs?q=absent").text


def test_log_navigation_restores_related_recording_and_comment_filters(client, paths):
    from live_rec_lab.comments.store import store_comment
    from live_rec_lab.config import utcnow
    from live_rec_lab.db.models import StreamSession
    from live_rec_lab.platforms.base import CommentIn
    from live_rec_lab.services.sessions import register_streamer

    now = utcnow()
    with session_scope() as db:
        streamer = register_streamer(
            db, platform="tiktok", account="only_comments", display_name="コメントのみ",
            monitoring=False, recording=False, comments=True,
        )
        stream = StreamSession(streamer_id=streamer.id, platform_stream_id="comments", detected_at=now, status="ENDED")
        db.add(stream)
        db.flush()
        session = stream.id
        store_comment(db, session, CommentIn(message="A&B", source="test"))
    row = {"timestamp": now.isoformat(), "level": "WARNING", "component": "test",
           "message": "A&B", "stream_session_id": session}
    (paths["state"] / "logs" / "app.jsonl.1").write_text(json.dumps(row) + "\n", encoding="utf-8")

    logs = client.get("/logs", params={"q": "A&B", "page": 1})
    link = re.search(r'href="([^"]+)">関連セッション', logs.text)[1]
    detail = client.get(link)
    assert f'href="/comments?session_id={session}"' in detail.text
    assert 'href="/logs?q=A%26B&amp;page=1"' in detail.text
    assert "字幕を再生成" not in detail.text
    comments = client.get(f"/comments?session_id={session}&q=A%26B")
    link = re.search(r'href="([^"]+)">コメントのみ', comments.text)[1]
    detail = client.get(link)
    assert f'href="/comments?session_id={session}&amp;q=A%26B"' in detail.text


def test_log_web_states_distinguish_unavailable_and_partial(client, paths):
    folder = paths["state"] / "logs"
    active = folder / "app.jsonl"
    active.write_text("破損行\n", encoding="utf-8")
    assert "一部のログを取得できませんでした" in client.get("/logs").text
    moved = folder.with_name("moved-logs")
    folder.rename(moved)
    try:
        assert "構造化ログを取得できませんでした" in client.get("/logs").text
    finally:
        moved.rename(folder)


def test_latest_recording_success_does_not_hide_previous_failure(client):
    from live_rec_lab.config import utcnow
    from live_rec_lab.db.models import CollectionGap, RecordingJob, RecordingSegment, StreamSession
    from live_rec_lab.services.sessions import register_streamer

    now = utcnow()
    with session_scope() as db:
        streamer = register_streamer(
            db, platform="tiktok", account="partial", display_name="欠損のある配信者",
            monitoring=False, recording=False, comments=False,
        )
        stream = StreamSession(streamer_id=streamer.id, platform_stream_id="42", detected_at=now, status="ENDED")
        db.add(stream)
        db.flush()
        failure = RecordingJob(stream_session_id=stream.id, status="FAILED")
        latest = RecordingJob(stream_session_id=stream.id, status="COMPLETED")
        db.add(failure)
        db.flush()
        db.add(latest)
        db.add(CollectionGap(
            stream_session_id=stream.id, job_type="recording", started_at=now,
            ended_at=now, reason="fixture gap",
        ))
        db.add(RecordingSegment(
            recording_job_id=failure.id, stream_session_id=stream.id, segment_number=2,
            started_at=now, validation_status="invalid",
        ))
    detail = client.get(f"/recordings/{stream.id}")
    assert "一部取得・要確認" in detail.text
    assert "最新録画ジョブ</dt><dd>完了" in detail.text
    assert "未確定・無効のセグメント 1本" in detail.text and "fixture gap" in detail.text
    api = client.get(f"/api/v1/sessions/{stream.id}").json()
    assert api["latest_recording_status"] == "COMPLETED"
    assert api["recording_status"] == "PARTIAL"
    assert api["failed_recording_jobs"] == 1 and api["recording_gap_count"] == 1


def test_archive_then_reactivate_preserves_history_and_requires_explicit_toggles(client):
    from live_rec_lab.config import utcnow
    from live_rec_lab.db.models import StreamSession

    token = client.get("/api/v1/health").headers["x-csrf-token"]
    headers = {"X-CSRF-Token": token}
    base = {"platform": "tiktok", "account": "restore",
            "monitoring_enabled": False, "recording_enabled": True, "comments_enabled": True}
    response = client.post("/api/v1/streamers", json=base, headers=headers)
    streamer_id = response.json()["id"]
    with session_scope() as db:
        stream = StreamSession(streamer_id=streamer_id, platform_stream_id="42", detected_at=utcnow(), status="ENDED")
        db.add(stream)
        db.flush()
    response = client.delete(f"/api/v1/streamers/{streamer_id}?confirm=delete", headers=headers)
    assert response.json()["archived"]
    assert client.get("/api/v1/streamers").json()["items"] == []
    response = client.post("/api/v1/streamers", json={"platform": "tiktok", "account": "restore"}, headers=headers)
    assert response.status_code == 422
    response = client.post("/api/v1/streamers", json={**base, "comments_enabled": False}, headers=headers)
    assert response.json()["id"] == streamer_id and response.json()["comments_enabled"] is False
    assert client.get(f"/recordings/{stream.id}").status_code == 200
