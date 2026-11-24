import re

from fastapi.testclient import TestClient

from live_rec_lab.collection import run_fixture
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.storage_audit import run_storage_audit
from live_rec_lab.web.app import create_fixture_app


def _client(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    origin = "http://127.0.0.1:8083"
    client = TestClient(create_fixture_app(workspace), base_url=origin)
    page = client.get("/settings")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)[1]
    return workspace, client, {"Origin": origin, "X-CSRF-Token": token}


def test_operations_settings_require_confirmation_and_enforce_revision(tmp_path):
    _, client, headers = _client(tmp_path)
    url = "/api/v1/settings/operations"
    values = client.get(url).json()
    update = {**values, "tiktok_per_minute": 2.5, "comment_retention_days": 10}
    assert client.put(url, json=update).status_code == 403
    assert client.put(url, json=update, headers=headers).status_code == 409
    saved = client.put(url, json={**update, "confirmed": True}, headers=headers)
    assert saved.json() == {**update, "revision": 1}
    assert client.put(url, json=update, headers=headers).status_code == 409
    assert client.put(url, json={**update, "tiktok_per_minute": True}, headers=headers).status_code == 422


def test_storage_audit_and_media_deletion_require_csrf(tmp_path):
    workspace, client, headers = _client(tmp_path)
    data = run_fixture(workspace)
    session = data["session_id"]
    audit_url = "/api/v1/system/storage-audits"
    deletion_url = f"/api/v1/sessions/{session}/media-deletion"
    assert client.get(audit_url).json()["latest"] is None
    assert client.post(audit_url, json={}).status_code == 403
    job = client.post(audit_url, headers=headers, json={})
    assert job.status_code == 202
    assert run_storage_audit(workspace)["state"] == "completed"
    assert client.get(audit_url).json()["last_completed"]["id"] == job.json()["id"]
    assert "前回完了" in client.get("/system").text
    assert client.post(deletion_url, json={"mode": "files", "confirmed": True}).status_code == 403
    deleted = client.post(deletion_url, headers=headers, json={"mode": "files", "confirmed": True})
    assert deleted.status_code == 200 and deleted.json()["state"] == "completed"
    assert client.get(deletion_url).json()["operation"]["state"] == "completed"
