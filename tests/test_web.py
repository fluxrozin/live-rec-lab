from fastapi.testclient import TestClient

from live_rec_lab.collection import run_fixture
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.web.app import create_fixture_app


def fixture_client(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)
    app = create_fixture_app(workspace, port=18083)
    return TestClient(app, base_url="http://127.0.0.1:18083"), workspace


def test_fixture_api_reads_sessions_without_accessing_media(tmp_path):
    client, workspace = fixture_client(tmp_path)
    before = workspace.database.read_bytes()

    response = client.get("/api/v1/sessions")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["recording_state"] == "completed"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert b"mp4" not in response.content
    assert client.get("/recordings/night-boat/example.mp4").status_code == 404
    assert workspace.database.read_bytes() == before


def test_fixture_api_rejects_ambiguous_host_and_cross_origin(tmp_path):
    client, _ = fixture_client(tmp_path)
    for headers in (
        {"Host": "different.example:18083"},
        [("Host", "127.0.0.1:18083"), ("Host", "localhost:18083")],
        {"Origin": "http://localhost:18083"},
        {"Origin": "https://external.example"},
    ):
        response = client.get("/api/v1/sessions", headers=headers)
        assert response.status_code == 403
        assert response.headers["x-frame-options"] == "DENY"
        assert response.json()["error"]["code"] in ("invalid_host", "invalid_origin")


def test_fixture_api_protects_mutating_methods_and_missing_routes(tmp_path):
    client, _ = fixture_client(tmp_path)
    response = client.post("/api/v1/sessions/1/stop")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_origin"
    response = client.post("/api/v1/sessions/1/stop", headers={"Origin": "http://127.0.0.1:18083"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_csrf"
    assert client.get("/api/v1/sessions/999").status_code == 404


def test_comment_search_escapes_wildcards_and_paginates(tmp_path):
    client, _ = fixture_client(tmp_path)
    request = "/api/v1/comments"
    count = client.get(request, params={"mode": "all", "q": "こんばんは", "page_size": 1})
    assert count.json()["total"] == 1
    assert "こんばんは" in count.json()["items"][0]["body"]
    assert client.get(request, params={"mode": "all", "q": "%"}).json()["total"] == 0
    second_page = client.get(request, params={"mode": "all", "page_size": 1, "page": 2}).json()
    assert second_page["items"][0]["body"] == "この曲好きです"
    assert second_page["has_next"] is False
    assert client.get(request, params={"mode": "all", "page_size": 201}).status_code == 422


def test_isolated_account_registration_preserves_identity_across_archiving(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    app = create_fixture_app(workspace, port=18083)
    client = TestClient(app, base_url="http://127.0.0.1:18083")
    headers = {"Origin": "http://127.0.0.1:18083", "X-CSRF-Token": app.state.csrf_token}
    record = {
        "platform": "tiktok",
        "target": "https://www.tiktok.com/@night_boat/live",
        "display_name": "夜舟",
        "monitoring_enabled": True,
        "recording_enabled": False,
        "comments_enabled": True,
    }

    initial = client.post("/api/v1/streamers", headers=headers, json=record)

    assert initial.status_code == 201
    account = initial.json()
    assert account["normalized_id"] == "night_boat"
    assert account["recording_enabled"] is False
    assert client.post("/api/v1/streamers", headers=headers, json=record).status_code == 409
    path = f"/api/v1/streamers/{account['id']}"
    assert client.request("DELETE", path, headers=headers, json={"confirmed": True}).status_code == 200
    assert client.get("/api/v1/streamers").json()["total"] == 0
    restored = client.post("/api/v1/streamers", headers=headers, json=record)
    assert restored.status_code == 201
    assert restored.json()["id"] == account["id"]


def test_registration_requires_all_three_boolean_switches(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    app = create_fixture_app(workspace, port=18083)
    client = TestClient(app, base_url="http://127.0.0.1:18083")
    headers = {"Origin": "http://127.0.0.1:18083", "X-CSRF-Token": app.state.csrf_token}
    incomplete = {"platform": "tiktok", "target": "night_boat", "monitoring_enabled": "yes"}
    response = client.post("/api/v1/streamers", headers=headers, json=incomplete)
    assert response.status_code == 422
    assert client.get("/api/v1/streamers").json()["total"] == 0
