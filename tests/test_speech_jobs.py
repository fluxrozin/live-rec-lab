import sqlite3
import wave
from contextlib import closing
from threading import Event

import pytest

from live_rec_lab.ass_jobs import process_ass
from live_rec_lab.collection import run_fixture
from live_rec_lab.db import connect
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.speech import SpeechBlocked, SpeechCue
from live_rec_lab.speech_jobs import enqueue_speech, process_speech, speech_attempts
from live_rec_lab.speech_worker import extract_audio


def recorded(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    result = run_fixture(workspace)
    return workspace, workspace.root / result["video"]


def test_queue_is_durable_and_repeated_enqueue_reuses_waiting_attempt(tmp_path):
    workspace, video = recorded(tmp_path)
    with closing(connect(workspace.database)) as database:
        database.execute("PRAGMA user_version = 1")
    first = enqueue_speech(workspace, 1)
    second = enqueue_speech(FixtureWorkspace.open(workspace.root), 1)
    assert second.id == first.id
    assert first.state == "queued"
    assert speech_attempts(workspace)[0].state == "queued"

    def infer(workspace_, attempt, source_path):
        assert source_path == video
        assert attempt.device == "cuda"
        with workspace_.exclusive(), closing(sqlite3.connect(workspace_.database)) as database:
            database.execute("BEGIN IMMEDIATE")
            database.rollback()
        return [SpeechCue(400, 900, "音声ジョブの試験")]

    done = process_speech(workspace, infer)
    assert done.state == "completed"
    assert speech_attempts(workspace)[0].id == first.id
    assert "音声ジョブの試験" in video.with_name(video.stem + "_speech.srt").read_text()
    assert not video.with_suffix(".ass").exists()
    assert process_ass(workspace).state == "completed"
    assert "音声ジョブの試験" in video.with_suffix(".ass").read_text()
    assert process_speech(workspace, infer) is None


def test_missing_model_is_blocked_without_creating_a_subtitle(tmp_path):
    workspace, video = recorded(tmp_path)
    enqueue_speech(workspace, 1)
    done = process_speech(workspace)
    assert done.state == "blocked"
    assert done.reason == "model_unavailable"
    assert not video.with_name(video.stem + "_speech.srt").exists()


def test_video_generation_is_rechecked_after_inference(tmp_path):
    workspace, video = recorded(tmp_path)
    enqueue_speech(workspace, 1)

    def switch_video(*_):
        video.write_bytes(b"externally substituted")
        return [SpeechCue(0, 900, "must not publish")]

    done = process_speech(workspace, switch_video)
    assert done.state == "needs_review"
    assert not video.with_name(video.stem + "_speech.srt").exists()
    assert video.read_bytes() == b"externally substituted"


def test_interrupted_attempt_is_requeued_without_reusing_trial_identity(tmp_path):
    workspace, _ = recorded(tmp_path)
    abandoned = enqueue_speech(workspace, 1)
    with closing(connect(workspace.database)) as database, database:
        database.execute("UPDATE speech_attempts SET state = 'running' WHERE id = ?", (abandoned.id,))

    done = process_speech(workspace, lambda *_: [])
    assert done.state == "no_speech"
    assert done.id != abandoned.id
    assert [job.state for job in speech_attempts(workspace)] == ["no_speech", "interrupted"]


def test_inference_failure_keeps_preexisting_subtitle(tmp_path):
    workspace, video = recorded(tmp_path)
    from live_rec_lab.postprocess import postprocess_fixture

    result = postprocess_fixture(workspace, "tiktok")
    saved = workspace.root / result["speech_srt"]
    old_text = saved.read_text()
    old_ass = video.with_suffix(".ass").read_text()
    enqueue_speech(workspace, 1)

    def unsupported(*_):
        raise SpeechBlocked("gpu unavailable")

    assert process_speech(workspace, unsupported).state == "blocked"
    assert saved.read_text() == old_text
    assert video.with_suffix(".ass").read_text() == old_ass


def test_extract_audio_from_managed_fixture_is_mono_16khz(tmp_path):
    workspace, video = recorded(tmp_path)
    output = workspace.events.parent / "extracted.wav"
    extract_audio(video, output)

    with wave.open(str(output), "rb") as audio:
        assert audio.getframerate() == 16000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getnframes() > 16_000
        assert audio.readframes(800) == bytes(800 * 2)
        audio.setpos(9000)
        assert any(audio.readframes(800))


def test_unsettled_segment_cannot_be_queued(tmp_path):
    workspace, _ = recorded(tmp_path)
    with closing(connect(workspace.database)) as database, database:
        database.execute("UPDATE stream_sessions SET stream_state = 'live'")
    with pytest.raises(ValueError, match="settled"):
        enqueue_speech(workspace, 1)


def test_retry_replaces_only_verified_prior_subtitles_and_can_finish_without_speech(tmp_path):
    from live_rec_lab.postprocess import postprocess_fixture

    workspace, video = recorded(tmp_path)
    result = postprocess_fixture(workspace, "tiktok")
    old_srt = workspace.root / result["speech_srt"]

    enqueue_speech(workspace, 1)
    revised = process_speech(workspace, lambda *_: [SpeechCue(500, 1000, "置き換えた発話")])
    assert revised.state == "completed"
    assert "置き換えた発話" in old_srt.read_text()
    assert process_ass(workspace).state == "completed"
    assert "置き換えた発話" in video.with_suffix(".ass").read_text()

    enqueue_speech(workspace, 1)
    no_speech = process_speech(workspace, lambda *_: [])
    assert no_speech.state == "no_speech"
    assert not old_srt.exists()
    assert process_ass(workspace).state == "completed"
    assert "こんばんは" in video.with_suffix(".ass").read_text()


def test_retry_leaves_an_unmanaged_subtitle_in_place(tmp_path):
    workspace, video = recorded(tmp_path)
    subtitle = video.with_name(video.stem + "_speech.srt")
    subtitle.write_text("external subtitle")

    enqueue_speech(workspace, 1)
    result = process_speech(workspace, lambda *_: [SpeechCue(500, 1000, "置換禁止")])
    assert result.state == "needs_review"
    assert subtitle.read_text() == "external subtitle"


def test_comment_expiry_blocks_ass_only_and_keeps_new_speech(tmp_path):
    from live_rec_lab.maintenance import expire_fixture_comments

    workspace, video = recorded(tmp_path)
    queued = enqueue_speech(workspace, 1)
    expire_fixture_comments(workspace, "tiktok")
    result = process_speech(workspace, lambda *_: [SpeechCue(500, 1000, "音声のみ")])

    assert result.id == queued.id
    assert result.state == "completed"
    assert process_ass(workspace, automatic=True) is None
    assert not video.with_suffix(".ass").exists()
    assert "音声のみ" in video.with_name(video.stem + "_speech.srt").read_text()


def test_cli_queues_and_reports_missing_models(tmp_path):
    import json

    from test_fixture import cli

    workspace, _ = recorded(tmp_path)
    queued = json.loads(cli("fixture", "speech-queue", workspace.root, "1").stdout)
    result = json.loads(cli("fixture", "speech-work", workspace.root).stdout)
    status = json.loads(cli("fixture", "speech-status", workspace.root).stdout)

    assert queued["state"] == "queued"
    assert result["id"] == queued["id"]
    assert result["state"] == "blocked"
    assert status["items"][0]["reason"] == "model_unavailable"


def test_prepared_revision_reconciles_after_file_was_published_before_database_commit(tmp_path, monkeypatch):
    from live_rec_lab import publication

    workspace, video = recorded(tmp_path)
    enqueue_speech(workspace, 1)
    old = process_speech(workspace, lambda *_: [SpeechCue(500, 1000, "初回")])
    assert old.state == "completed"

    original = publication._commit_revision
    interrupted = False

    def fault(database, operation, version):
        nonlocal interrupted
        if not interrupted and operation["kind"] == "speech":
            interrupted = True
            raise sqlite3.OperationalError("simulated database failure after file swap")
        return original(database, operation, version)

    monkeypatch.setattr(publication, "_commit_revision", fault)
    enqueue_speech(workspace, 1)
    failure = process_speech(workspace, lambda *_: [SpeechCue(500, 1000, "公開途中")])
    assert failure.state == "needs_review"
    with closing(connect(workspace.database)) as db:
        row = db.execute("SELECT backup_path FROM subtitle_revision_operations WHERE state = 'prepared'").fetchone()
        assert "初回" in (workspace.root / row["backup_path"]).read_text()

    enqueue_speech(workspace, 1)
    recovered = process_speech(workspace, lambda *_: [SpeechCue(500, 1000, "照合後")])
    assert recovered.state == "completed"
    assert "照合後" in video.with_name(video.stem + "_speech.srt").read_text()
    assert not list(video.parent.glob(".subtitle-previous-*"))


def test_offline_worker_distinguishes_no_audio_from_missing_model(tmp_path):
    from live_rec_lab.media import generation, media_tool

    workspace, video = recorded(tmp_path)
    silent = video.with_name("silent-fixture.mp4")
    media_tool(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-protocol_whitelist",
            "file",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            str(silent),
        ],
    )
    with closing(connect(workspace.database)) as database, database:
        database.execute(
            "UPDATE recording_segments SET video_path = ?, video_generation = ? WHERE id = 1",
            (str(silent.relative_to(workspace.root)), generation(silent)),
        )
    enqueue_speech(workspace, 1)
    result = process_speech(workspace)
    assert result.state == "not_applicable" and result.reason == "no_audio"


def test_extraction_pads_late_audio_stream_relative_to_video_start(tmp_path):
    from live_rec_lab.media import media_tool

    video = tmp_path / "late-audio.mp4"
    media_tool(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            "-threads",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(video),
        ],
    )
    output = tmp_path / "mono.wav"
    extract_audio(video, output)

    with wave.open(str(output), "rb") as audio:
        assert audio.readframes(4000) == bytes(8000)
        audio.setpos(12000)
        assert any(audio.readframes(800))


def test_explicit_request_can_run_a_queued_automatic_job_when_automatic_is_off(tmp_path):
    from live_rec_lab.speech_jobs import SpeechConfiguration, save_speech_configuration

    workspace, _ = recorded(tmp_path)
    run_fixture(workspace, "twitcasting")
    process_speech(workspace, lambda *_: [], automatic=True)
    waiting = next(job for job in speech_attempts(workspace) if job.state == "queued")
    save_speech_configuration(workspace, SpeechConfiguration(enabled=False, device="cpu"))

    assert process_speech(workspace, lambda *_: [], automatic=True) is None
    manual = enqueue_speech(workspace, waiting.segment_id)

    assert manual.id == waiting.id
    assert manual.origin == "manual" and manual.device == "cpu"
    assert process_speech(workspace, lambda *_: []).state == "no_speech"


def test_speech_stop_does_not_publish_and_automatic_recovery_is_bounded(tmp_path):
    workspace, video = recorded(tmp_path)
    enqueue_speech(workspace, 1)

    for depth in range(3):
        stop = Event()

        def interrupt(*_):
            stop.set()
            return [SpeechCue(500, 1000, "stopped before publication")]

        stopped = process_speech(workspace, interrupt, stop=stop)
        assert stopped.state == "interrupted"
        assert stopped.recovery_depth == depth
        assert not video.with_name(video.stem + "_speech.srt").exists()

    assert process_speech(workspace, lambda *_: []) is None
    assert speech_attempts(workspace)[0].state == "failed"
    assert speech_attempts(workspace)[0].reason == "automatic_retry_exhausted"


def test_cuda_child_uses_only_library_dirs_from_current_interpreter(tmp_path, monkeypatch):
    from live_rec_lab import speech_worker

    site = tmp_path / "site-packages"
    cublas = site / "nvidia" / "cublas" / "lib"
    cudnn = site / "nvidia" / "cudnn" / "lib"
    cublas.mkdir(parents=True)
    cudnn.mkdir(parents=True)
    monkeypatch.setattr(speech_worker.sysconfig, "get_path", lambda key: str(site))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/outside")

    assert speech_worker._cuda_library_dirs().split(":") == [str(cublas), str(cudnn)]


def test_speech_stop_reaps_owned_child(tmp_path, monkeypatch):
    import sys
    from threading import Timer

    from live_rec_lab import speech_worker

    workspace, video = recorded(tmp_path)
    enqueue_speech(workspace, 1)
    stop = Event()
    children = []
    spawn = speech_worker.subprocess.Popen
    timer = Timer(0.1, stop.set)

    def use_waiting_child(_, **options):
        child = spawn([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **options)
        children.append(child)
        timer.start()
        return child

    monkeypatch.setattr(speech_worker.subprocess, "Popen", use_waiting_child)
    try:
        result = process_speech(workspace, stop=stop)
    finally:
        timer.cancel()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()

    assert result.state == "interrupted"
    assert len(children) == 1 and children[0].returncode is not None
    assert not video.with_name(video.stem + "_speech.srt").exists()
