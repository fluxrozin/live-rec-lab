import re

from live_rec_lab.ass_jobs import process_ass
from live_rec_lab.collection import run_fixture
from live_rec_lab.maintenance import expire_fixture_comments
from live_rec_lab.postprocess import postprocess_fixture
from live_rec_lab.speech_jobs import enqueue_speech
from test_pages import client_for


def test_ass_submission_shows_queued_and_preserves_current_version(tmp_path):
    client, workspace = client_for(tmp_path, populate=True)
    initial = client.get("/speech")
    assert "統合ASS再生成を予約" in initial.text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', initial.text)[1]
    headers = {"Origin": "http://127.0.0.1:18083", "X-CSRF-Token": token}
    path = "/api/v1/segments/1/ass"

    assert client.post(path, json={}).status_code == 403
    first = client.post(path, headers=headers, json={})
    assert first.status_code == 202 and first.json()["state"] == "queued"
    second = client.post(path, headers=headers, json={})
    assert second.json()["id"] == first.json()["id"]
    scheduled = client.get("/speech").text
    assert "待機" in scheduled and "保存済み" in scheduled
    assert "統合ASS再生成を予約" not in scheduled

    assert process_ass(workspace).state == "completed"
    assert "統合ASS再生成を予約" in client.get("/speech").text
    expire_fixture_comments(workspace, "tiktok")
    assert client.post(path, headers=headers, json={}).status_code == 409
    expired = client.get("/speech").text
    assert "期限削除済み" in expired and "統合ASS再生成を予約" not in expired


def test_speech_cues_filter_by_the_selected_recording_segment(tmp_path):
    client, workspace = client_for(tmp_path, populate=True)
    run_fixture(workspace, "twitcasting")
    postprocess_fixture(workspace, "twitcasting")

    selected = client.get("/speech/cues", params={"session_id": 1, "segment_id": 1, "q": "発話"})
    assert selected.status_code == 200
    assert "録画セグメント" in selected.text and "架空の発話" in selected.text
    assert 'value="1" selected' in selected.text
    mismatch = client.get("/speech/cues", params={"session_id": 1, "segment_id": 2})
    assert "条件に一致する発話がありません" in mismatch.text
    selected_only = client.get("/speech/cues", params={"segment_id": 2})
    assert 'name="session_id" value="2"' in selected_only.text
    assert "架空の発話" in selected_only.text


def test_subtitle_attempt_filters_distinguish_speech_and_ass_during_pagination(tmp_path):
    client, workspace = client_for(tmp_path, populate=True)
    run_fixture(workspace, "twitcasting")
    enqueue_speech(workspace, 2)
    assert process_ass(workspace, automatic=True).state == "completed"

    all_rows = client.get("/speech").text
    assert 'id="speech-queue-counts"' in all_rows and 'id="ass-queue-counts"' in all_rows
    assert 'href="/recordings/1"' in all_rows and 'href="/recordings/2"' in all_rows
    speech = client.get("/speech", params={"speech_state": "queued"}).text
    assert 'href="/recordings/2"' in speech and 'href="/recordings/1"' not in speech
    ass = client.get("/speech", params={"ass_state": "completed"}).text
    assert 'href="/recordings/1"' in ass and 'href="/recordings/2"' not in ass
