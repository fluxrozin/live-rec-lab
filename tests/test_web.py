"""Web: 8画面の描画・HTTP防御・設定保存・配信者操作（実DB・TestClient、外部通信なし）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from live_rec_lab import sessions, storage, streamers
from live_rec_lab.app import create_app
from live_rec_lab.db.models import recording_jobs, recording_segments


def _client(ctx):
    ctx.services["adapters"] = {}
    ctx.scheduler_disabled = True
    app = create_app(ctx)
    return TestClient(app, base_url=f"http://127.0.0.1:{ctx.port}")


def test_recording_size_grows_before_segment_is_finalized(ctx):
    root = ctx.env.recording_root
    raw = root / "sample" / "2026-11-25" / f".rec-a123456789.raw{storage.TMP_SUFFIX}"
    raw.parent.mkdir(parents=True)
    with ctx.engine.begin() as conn:
        streamer = streamers.register(conn, streamers.StreamerInput("tiktok", "sample", None, True, True, False))
        session, _ = sessions.open_session(conn, streamer, "room", "架空配信", None, root)
        sid = session["id"]
        job = sessions.create_recording_job(conn, sid, None)
        sessions.update_job(conn, recording_jobs, job, status="RECORDING")
        conn.execute(recording_segments.insert().values(
            recording_job_id=job, stream_session_id=sid, segment_number=1, file_path="sample/previous.mp4",
            file_size=1024, validation_status="valid", created_at=session["created_at"]))
        pending_id = sessions.add_pending_segment(conn, job, sid, "streamlink", str(raw), "a123456789")

    with _client(ctx) as client:
        partial = "/partials/active-sessions"
        assert "1.0 KiB" in client.get(partial).text
        raw.write_bytes(bytes(1024))
        assert "2.0 KiB" in client.get(partial).text
        with raw.open("ab") as file:
            file.write(bytes(2048))
        for path in ("/", partial, "/recordings", f"/recordings/{sid}"):
            response = client.get(path)
            assert response.status_code == 200
            assert "4.0 KiB" in response.text, path
        assert "3.0 KiB" in client.get(f"/recordings/{sid}").text
        assert client.get("/api/v1/dashboard").json()["managed_bytes"] == 1024

        with ctx.engine.begin() as conn:
            conn.execute(recording_segments.update().where(recording_segments.c.id == pending_id).values(
                segment_number=2, file_size=4096, validation_status="valid", temp_path=None))
        assert "5.0 KiB" in client.get(partial).text


def test_pages_security_and_settings(ctx):
    with ctx.engine.begin() as conn:
        streamers.register(conn, streamers.StreamerInput("tiktok", "web_sample", "<b>架空</b>", True, True, True))
    with _client(ctx) as c:
        h = {"Origin": f"http://127.0.0.1:{ctx.port}", "X-CSRF-Token": ctx.csrf_token}
        for path in ("/", "/streamers", "/recordings", "/comments", "/comments?mode=session", "/speech", "/system", "/logs", "/settings"):
            r = c.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
            assert "content-security-policy" in r.headers
        assert "&lt;b&gt;架空&lt;/b&gt;" in c.get("/streamers").text  # 表示名はHTMLとして解釈されない
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
        # 防御
        assert c.get("/", headers={"Host": "evil.example"}).status_code == 403
        assert c.post("/api/v1/streamers", json={}).status_code == 403  # Origin/CSRFなし
        assert c.post("/api/v1/streamers", json={}, headers={"Origin": h["Origin"]}).status_code == 403  # CSRFなし
        # 設定: 版・検証・原子性
        cur = c.get("/api/v1/settings").json()
        ver = cur["version"]
        bad = c.put("/api/v1/settings", json={"settings": {"web_port": 80, "display_font": "noto-sans-jp"}, "version": ver}, headers=h)
        assert bad.status_code == 422 and "web_port" in bad.json()["error"]["fields"]
        assert c.get("/api/v1/settings").json()["settings"]["display_font"] == "windows"  # 一部だけ保存されない
        ok = c.put("/api/v1/settings", json={"settings": {"display_font": "noto-sans-jp", "web_port": 8090}, "version": ver}, headers=h)
        assert ok.status_code == 200 and ok.json()["version"] == ver + 1
        assert c.put("/api/v1/settings", json={"settings": {"display_font": "windows"}, "version": ver}, headers=h).status_code == 409
        assert c.put("/api/v1/settings", json={"settings": {"SIGN_API_KEY": "x"}, "version": ver + 1}, headers=h).status_code == 422
        assert 'data-font="noto-sans-jp"' in c.get("/").text
        # 配信者: 登録・重複・設定変更・削除確認
        r = c.post("/api/v1/streamers", json={"platform": "tiktok", "account": "https://www.tiktok.com/@api_one", "monitoring_enabled": True,
                                               "recording_enabled": False, "comments_enabled": True}, headers=h)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        assert c.post("/api/v1/streamers", json={"platform": "tiktok", "account": "@api_one", "monitoring_enabled": True,
                                                  "recording_enabled": True, "comments_enabled": True}, headers=h).status_code == 409
        assert c.post("/api/v1/streamers", json={"platform": "tiktok", "account": "@x"}, headers=h).status_code == 422
        assert c.patch(f"/api/v1/streamers/{sid}", json={"recording_enabled": True}, headers=h).json()["recording_enabled"] is True
        assert c.patch(f"/api/v1/streamers/{sid}", json={"platform": "twitcasting"}, headers=h).status_code == 422
        assert c.delete(f"/api/v1/streamers/{sid}", headers=h).status_code == 422
        assert c.delete(f"/api/v1/streamers/{sid}?confirm=true", headers=h).status_code == 200
        order = c.get("/api/v1/streamers/order").json()
        assert c.put("/api/v1/streamers/order", json={"ordered_ids": order["ordered_ids"], "order_revision": 999}, headers=h).status_code == 409
