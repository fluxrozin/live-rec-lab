from contextlib import closing

import pytest

from live_rec_lab import ass_jobs
from live_rec_lab.collection import run_fixture
from live_rec_lab.db import connect
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.maintenance import expire_fixture_comments
from live_rec_lab.speech import SpeechCue
from live_rec_lab.speech_jobs import enqueue_speech, process_speech


def _recorded(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    result = run_fixture(workspace)
    return workspace, (workspace.root / result["video"]).with_suffix(".ass")


def test_comment_only_ass_updates_after_independent_speech_job(tmp_path):
    workspace, ass = _recorded(tmp_path)

    initial = ass_jobs.process_ass(workspace, automatic=True)
    assert initial.state == "provisional"
    assert "こんばんは" in ass.read_text()
    assert initial.reason == "speech_pending"
    assert ass_jobs.process_ass(workspace, automatic=True) is None

    enqueue_speech(workspace, 1)
    speech = process_speech(workspace, lambda *_: [SpeechCue(300, 900, "後着の発話")])
    assert speech.state == "completed"
    assert "後着の発話" not in ass.read_text()
    with closing(connect(workspace.database)) as database:
        assert ass_jobs.latest_ass(database, 1).state == "queued"

    revised = ass_jobs.process_ass(workspace)
    assert revised.id != initial.id
    assert revised.state == "completed"
    assert "後着の発話" in ass.read_text()
    assert "こんばんは" in ass.read_text()


def test_ass_publication_is_rejected_if_comments_expire_while_rendering(tmp_path, monkeypatch):
    workspace, ass = _recorded(tmp_path)
    first = ass_jobs.enqueue_ass(workspace, 1)
    assert ass_jobs.enqueue_ass(workspace, 1) == first
    dimensions = ass_jobs.fixture_dimensions

    def expire(video, **options):
        assert expire_fixture_comments(workspace, "tiktok")["state"] == "expired"
        return dimensions(video, **options)

    monkeypatch.setattr(ass_jobs, "fixture_dimensions", expire)
    result = ass_jobs.process_ass(workspace)
    assert result.state == "expired"
    assert not ass.exists()
    assert ass_jobs.process_ass(workspace, automatic=True) is None
    with pytest.raises(ValueError, match="expired"):
        ass_jobs.enqueue_ass(workspace, 1)


def test_ass_rechecks_expiration_marker_after_rendering(tmp_path, monkeypatch):
    workspace, ass = _recorded(tmp_path)
    ass_jobs.enqueue_ass(workspace, 1)
    dimensions = ass_jobs.fixture_dimensions

    def reserve_expiration(video, **options):
        # Inject a recovery marker just after input capture to exercise the second validation.
        with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
            database.execute("""
                INSERT INTO comment_expirations(session_id, state, started_at)
                VALUES (1, 'running', '2026-11-24T12:00:00+00:00')
            """)
        return dimensions(video, **options)

    monkeypatch.setattr(ass_jobs, "fixture_dimensions", reserve_expiration)
    assert ass_jobs.process_ass(workspace).state == "expired"
    assert not ass.exists()


def test_ass_preserves_previous_version_if_input_generation_changes(tmp_path):
    workspace, ass = _recorded(tmp_path)
    initial = ass_jobs.process_ass(workspace, automatic=True)
    old = ass.read_bytes()
    ass_jobs.enqueue_ass(workspace, 1)
    ass.with_name(ass.stem + "_comment.srt").write_text("external replacement")

    held = ass_jobs.process_ass(workspace)
    assert held.id != initial.id
    assert held.state == "needs_review"
    assert ass.read_bytes() == old


def test_automatic_ass_uses_prior_speech_if_a_new_attempt_fails(tmp_path):
    workspace, ass = _recorded(tmp_path)
    enqueue_speech(workspace, 1)
    process_speech(workspace, lambda *_: [SpeechCue(300, 900, "最後の正常な発話")])
    ass_jobs.process_ass(workspace)

    enqueue_speech(workspace, 1)

    def fail(*_):
        raise RuntimeError("simulated recognition failure")

    assert process_speech(workspace, fail).state == "failed"
    updated = ass_jobs.process_ass(workspace, automatic=True)

    assert updated.state == "partial"
    assert updated.reason == "speech_previous_result"
    assert "最後の正常な発話" in ass.read_text()


def test_ass_rechecks_exact_srt_generation_immediately_before_replacing_old_version(tmp_path, monkeypatch):
    from live_rec_lab import publication

    workspace, ass = _recorded(tmp_path)
    assert ass_jobs.process_ass(workspace, automatic=True).state == "provisional"
    old = ass.read_bytes()
    ass_jobs.enqueue_ass(workspace, 1)
    write_staged = publication._write_staged

    def change_input(directory, contents, label):
        path = write_staged(directory, contents, label)
        if label == "subtitle-stage":
            ass.with_name(ass.stem + "_comment.srt").write_text("external replacement")
        return path

    monkeypatch.setattr(publication, "_write_staged", change_input)
    result = ass_jobs.process_ass(workspace)
    assert result.state == "needs_review"
    assert result.reason == "publication_failed"
    assert ass.read_bytes() == old


def test_ass_dimensions_follow_video_display_rotation(tmp_path):
    from live_rec_lab.media import fixture_dimensions, media_tool

    workspace, ass = _recorded(tmp_path)
    with closing(connect(workspace.database)) as database:
        source = workspace.media_path(database.execute("SELECT video_path FROM recording_segments").fetchone()[0])
    rotated = ass.with_name("rotate-90.mp4")
    width, height = fixture_dimensions(source)
    media_tool(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-protocol_whitelist",
            "file",
            "-display_rotation:v:0",
            "90",
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            str(rotated),
        ],
    )
    assert fixture_dimensions(rotated) == (height, width)
