import json
import sqlite3
import subprocess
import sys

import pytest

from live_rec_lab import collection
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.subtitles import srt_timestamp


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "live_rec_lab.cli", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_tiktok_cli_records_local_hls_and_writes_timed_comments(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    monkeypatch.setenv("LIVE_REC_LAB_RECORDING_ROOT", str(tmp_path / "unmanaged"))
    assert json.loads(cli("fixture", "init", root).stdout)["mode"] == "fixture"

    summary = json.loads(cli("fixture", "run", root).stdout)

    assert summary["stream_state"] == "ended"
    assert summary["recording_state"] == "completed"
    assert summary["comment_state"] == "completed"
    assert summary["comment_count"] == 2
    assert summary["subtitle_state"] == "completed"
    video, subtitle = (root / summary[key] for key in ("video", "comment_srt"))
    assert video.name == "tiktok_night-boat_20261124_211800_夜の選曲室_s001.mp4"
    assert video.stat().st_size > 0
    assert subtitle.stem == video.stem + "_comment"
    with sqlite3.connect(root / "data" / "fixture.sqlite3") as db:
        duration_ms = db.execute("SELECT duration_ms FROM recording_segments").fetchone()[0]
        assert 1990 <= duration_ms <= 2050
        assert f"00:00:00,450 --> {srt_timestamp(duration_ms)}" in subtitle.read_text()
        assert db.execute("SELECT COUNT(*) FROM comments").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM stream_sessions").fetchone() == (1,)
    assert json.loads(cli("fixture", "run", root).stdout) == summary
    assert not (tmp_path / "unmanaged").exists()


def test_recording_failure_still_retains_comments(tmp_path, monkeypatch):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")

    def broken_recording(*args):
        raise ValueError("recording failed in fixture")

    monkeypatch.setattr(collection, "record_fixture_video", broken_recording)
    result = collection.run_fixture(workspace)
    assert result["recording_state"] == "failed"
    assert result["comment_state"] == "completed"
    assert result["comment_count"] == 2
    assert result["subtitle_state"] == "blocked"


def test_comment_failure_does_not_publish_empty_srt(tmp_path, monkeypatch):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")

    def broken_comments():
        raise ValueError("comment fixture failed")

    monkeypatch.setattr(collection, "fixture_comments", broken_comments)
    result = collection.run_fixture(workspace)
    assert result["recording_state"] == "completed"
    assert result["comment_state"] == "failed"
    assert result["comment_count"] is None
    assert result["subtitle_state"] == "blocked"
    assert not list(workspace.recordings.rglob("*.srt"))


def test_confirmed_empty_comments_skip_subtitle(tmp_path, monkeypatch):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    monkeypatch.setattr(collection, "fixture_comments", list)

    result = collection.run_fixture(workspace)
    assert result["comment_count"] == 0
    assert result["subtitle_state"] == "skipped"
    assert not list(workspace.recordings.rglob("*.srt"))


def test_unknown_probe_is_not_offline(tmp_path, monkeypatch):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    monkeypatch.setattr(collection, "observe_fixture", lambda: None)

    result = collection.run_fixture(workspace)
    assert result["stream_state"] == "unknown"
    assert result["comment_count"] is None
    assert not list(workspace.recordings.rglob("*.mp4"))


def test_changed_media_is_held_instead_of_reported_as_completed(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    result = collection.run_fixture(workspace)
    video = workspace.root / result["video"]
    video.write_bytes(b"new contents")

    diagnosed = collection.run_fixture(workspace)

    assert diagnosed["recording_state"] == "needs_review"
    assert diagnosed["subtitle_state"] == "needs_review"
    assert video.read_bytes() == b"new contents"


def test_fixture_rejects_live_switch(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as rejection:
        cli("fixture", "run", tmp_path / "fixture", "--allow-live")
    assert "unrecognized arguments: --allow-live" in rejection.value.stderr


def test_twitcasting_uses_separate_session_and_shared_streamer_folder(tmp_path):
    root = tmp_path / "fixture"
    cli("fixture", "init", root)

    twitcasting = json.loads(cli("fixture", "run", root, "--service", "twitcasting").stdout)
    tiktok = json.loads(cli("fixture", "run", root).stdout)

    assert twitcasting["recording_state"] == "completed"
    assert twitcasting["comment_count"] == 2
    assert twitcasting["subtitle_state"] == "completed"
    assert (root / twitcasting["video"]).parent == (root / tiktok["video"]).parent
    assert (root / twitcasting["video"]).name.startswith("twitcasting_")
    with sqlite3.connect(root / "data" / "fixture.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM stream_sessions").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM streamers").fetchone() == (2,)


def test_interrupted_fixture_session_does_not_claim_completion(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    with sqlite3.connect(workspace.database) as db:
        db.execute("INSERT INTO streamers (id, platform, normalized_id, enabled) VALUES (1, 'tiktok', 'night-boat', 1)")
        db.execute("""
            INSERT INTO stream_sessions (streamer_id, source_id, title, started_at, stream_state)
            VALUES (1, 'synthetic-tiktok-001', '夜の選曲室', '2026-11-24T12:18:00+00:00', 'live')
        """)

    result = collection.run_fixture(workspace)

    assert result["stream_state"] == "interrupted"
    assert result["recording_state"] == "interrupted"
    assert result["comment_count"] is None


def test_fixture_postprocessing_publishes_verified_speech_and_ass(tmp_path):
    root = tmp_path / "fixture"
    cli("fixture", "init", root)
    collected = json.loads(cli("fixture", "run", root).stdout)

    processed = json.loads(cli("fixture", "postprocess", root).stdout)

    audio = root / processed["speech_srt"]
    combined = root / processed["ass"]
    video = root / collected["video"]
    assert audio.name == video.stem + "_speech.srt"
    assert combined.name == video.stem + ".ass"
    assert "架空の発話" in audio.read_text()
    assert "Speech" in combined.read_text() and "Comment" in combined.read_text()
    assert processed["inference"] == "synthetic"
    assert processed == json.loads(cli("fixture", "postprocess", root).stdout)


def test_fixture_postprocessing_holds_if_video_generation_changed(tmp_path):
    root = tmp_path / "fixture"
    cli("fixture", "init", root)
    collected = json.loads(cli("fixture", "run", root).stdout)
    (root / collected["video"]).write_bytes(b"changed after recording")

    with pytest.raises(subprocess.CalledProcessError) as rejected:
        cli("fixture", "postprocess", root)

    assert "generation" in rejected.value.stderr
    assert not list(root.rglob("*.ass"))


def test_fixture_rechecks_completed_states_when_publication_records_are_missing(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    collected = collection.run_fixture(workspace)
    with sqlite3.connect(workspace.database) as database:
        database.execute("DELETE FROM subtitle_publications")
        database.execute("DELETE FROM recording_segments")

    result = collection.run_fixture(workspace)

    assert result["recording_state"] == "needs_review"
    assert result["subtitle_state"] == "needs_review"
    assert (workspace.root / collected["video"]).is_file()
    assert (workspace.root / collected["comment_srt"]).is_file()
