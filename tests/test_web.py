"""Web: 8画面の描画・HTTP防御・設定保存・配信者操作（実DB・TestClient、外部通信なし）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from live_rec_lab import streamers
from live_rec_lab.app import create_app


def _client(ctx):
    ctx.services["adapters"] = {}
    ctx.scheduler_disabled = True
    app = create_app(ctx)
    return TestClient(app, base_url=f"http://127.0.0.1:{ctx.port}")


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
