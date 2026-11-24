import re

from fastapi.testclient import TestClient

from live_rec_lab.collection import run_fixture
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.postprocess import postprocess_fixture
from live_rec_lab.web.app import create_fixture_app


def client_for(tmp_path, *, populate=False):
    workspace = FixtureWorkspace.initialize(tmp_path / "ui")
    if populate:
        run_fixture(workspace)
        postprocess_fixture(workspace, "tiktok")
    return TestClient(create_fixture_app(workspace, port=18083), base_url="http://127.0.0.1:18083"), workspace


def test_approved_operations_layout_and_eight_pages(tmp_path):
    client, workspace = client_for(tmp_path)
    database = workspace.database.read_bytes()
    paths = {
        "/": "ダッシュボード",
        "/streamers": "配信者一覧",
        "/recordings": "録画履歴",
        "/comments": "コメント",
        "/speech": "音声字幕",
        "/system": "システム状態",
        "/logs": "ログ",
        "/settings": "設定",
    }
    for path, title in paths.items():
        response = client.get(path)
        assert response.status_code == 200
        assert f"<h1>{title}</h1>" in response.text
        assert re.search(r'aria-current="page"[^>]*>\s*' + title, response.text)
        assert response.text.count('aria-current="page"') == 1
        assert "隔離データ" in response.text
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert "no-store" in response.headers["cache-control"]
    assert "稼働中セッション" in client.get("/").text
    assert "実配信は未観測" in client.get("/").text
    assert client.get("/static/app.css").status_code == 200
    assert workspace.database.read_bytes() == database


def test_session_links_and_existing_fixture_searches(tmp_path):
    client, _ = client_for(tmp_path, populate=True)

    assert "/recordings/1" in client.get("/recordings", params={"platform": "tiktok", "q": "選曲室"}).text
    assert "対象の配信セッションがありません" in client.get("/recordings", params={"q": "存在しない"}).text
    detail = client.get("/recordings/1").text
    assert "夜の選曲室" in detail
    assert "コメントSRT" in detail and "統合ASS" in detail
    assert "架空の発話" in client.get("/speech/cues", params={"q": "発話"}).text
    filtered = client.get("/comments", params={"mode": "all", "q": "こんばんは"}).text
    assert "こんばんは 🌙" in filtered and "この曲好きです" not in filtered
    assert "synthetic-tiktok-001" in client.get("/logs", params={"q": "synthetic-tiktok-001"}).text
    assert client.get("/recordings/999").status_code == 404


def test_display_settings_are_persistent_and_versioned(tmp_path):
    client, workspace = client_for(tmp_path)
    page = client.get("/settings")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)[1]
    header = {"Origin": "http://127.0.0.1:18083", "X-CSRF-Token": token}
    settings = {"font": "noto", "display_timezone": "UTC", "refresh_seconds": 9, "revision": 0}

    assert client.put("/api/v1/settings/display", json=settings).status_code == 403
    assert client.put("/api/v1/settings/display", json=settings, headers=header).status_code == 200
    assert client.put("/api/v1/settings/display", json=settings, headers=header).status_code == 409
    assert (
        client.put(
            "/api/v1/settings/display", json={**settings, "display_timezone": "../etc"}, headers=header
        ).status_code
        == 422
    )

    reopened = TestClient(create_fixture_app(workspace, port=18083), base_url="http://127.0.0.1:18083")
    assert 'data-font="noto"' in reopened.get("/").text
    assert reopened.get("/api/v1/settings/display").json() == {**settings, "revision": 1}


def test_speech_settings_and_queue_are_isolated_and_csrf_protected(tmp_path):
    client, _ = client_for(tmp_path, populate=True)
    page = client.get("/settings")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)[1]
    headers = {"Origin": "http://127.0.0.1:18083", "X-CSRF-Token": token}
    values = {"enabled": False, "model": "large-v3-turbo", "language": "ja", "device": "cpu", "revision": 0}

    assert client.put("/api/v1/settings/speech", headers=headers, json=values).json() == {**values, "revision": 1}
    assert client.put("/api/v1/settings/speech", headers=headers, json=values).status_code == 409
    assert client.post("/api/v1/segments/1/speech", json={}).status_code == 403
    job = client.post("/api/v1/segments/1/speech", headers=headers, json={})
    assert job.status_code == 202
    assert job.json()["state"] == "queued" and job.json()["device"] == "cpu"
    again = client.post("/api/v1/segments/1/speech", headers=headers, json={})
    assert again.json()["id"] == job.json()["id"]
    assert "待機" in client.get("/speech").text


def test_web_port_setting_preserves_current_listener_and_checks_csrf_and_revision(tmp_path):
    client, workspace = client_for(tmp_path)
    page = client.get("/settings")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)[1]
    headers = {"Origin": "http://127.0.0.1:18083", "X-CSRF-Token": token}
    request = {"web_port": 19083, "revision": 0}

    assert client.put("/api/v1/settings/web-port", json=request).status_code == 403
    changed = client.put("/api/v1/settings/web-port", headers=headers, json=request)
    assert changed.status_code == 200
    assert changed.json() == {"web_port": 19083, "revision": 1, "current_port": 18083, "restart_required": True}
    assert client.put("/api/v1/settings/web-port", headers=headers, json=request).status_code == 409
    assert (
        client.put("/api/v1/settings/web-port", headers=headers, json={"web_port": 0, "revision": 1}).status_code == 422
    )
    assert client.get("/api/v1/health").status_code == 200
    assert "次回起動" in client.get("/settings").text
    restarted = TestClient(create_fixture_app(workspace, port=19083), base_url="http://127.0.0.1:19083")
    assert restarted.get("/api/v1/settings/web-port").json()["restart_required"] is False
