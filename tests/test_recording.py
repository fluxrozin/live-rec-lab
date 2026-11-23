import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import CollectionGap, RecordingJob, RecordingSegment, Streamer, StreamSession
from live_rec_lab.db.session import session_scope
from live_rec_lab.platforms.base import Snapshot
from live_rec_lab.recording.engines import build_command, installed_ffmpeg_dir, media_binary, run_command
from live_rec_lab.recording.hls_playlist import HlsMirror, filter_preroll_playlist, pick_variant
from live_rec_lab.recording.manager import (
    RecordingManager,
    _finalize_open,
    _finish,
    _initial_segment_number,
    _manual,
    _open_attempt,
    _schedule_retry,
    counts_as_recording_failure,
    engine_switch_notice,
    next_segment_number,
    reconnect_action,
    recording_continues,
    recording_engines,
    recording_exit_kind,
    session_filename_time,
)
from live_rec_lab.recording.segments import (
    Probe,
    attempt_is_empty,
    describe_probe_failure,
    finalize_segment,
    probe_media,
    remux_args,
    reserve_segment_path,
    unlink_empty_files,
)
from live_rec_lab.services.jobs import MonitorDetails
from live_rec_lab.services.sessions import apply_snapshot
from live_rec_lab.storage import broadcast_filename_time, recording_paths


def _executable(folder: Path, name: str) -> None:
    path = folder / name
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)


def test_installed_ffmpeg_9_1_is_preferred(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    _executable(folder, "ffmpeg")
    _executable(folder, "ffprobe")
    monkeypatch.setattr("live_rec_lab.recording.engines.installed_ffmpeg_dir", lambda: folder)
    monkeypatch.setattr(
        "live_rec_lab.recording.engines.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    assert media_binary("ffmpeg") == str(folder / "ffmpeg")
    assert media_binary("ffprobe") == str(folder / "ffprobe")
    command = build_command("ffmpeg", "https://example.test/live.m3u8", tmp_path / "out.ts", "best")
    assert command[0] == str(folder / "ffmpeg")


def test_incomplete_explicit_ffmpeg_pair_blocks_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "live_rec_lab.recording.engines.installed_ffmpeg_dir",
        lambda: tmp_path / "missing",
    )
    monkeypatch.setattr(
        "live_rec_lab.recording.engines.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    assert media_binary("ffprobe") is None
    only = tmp_path / "half"
    only.mkdir()
    _executable(only, "ffmpeg")
    monkeypatch.setattr("live_rec_lab.recording.engines.installed_ffmpeg_dir", lambda: only)
    assert media_binary("ffmpeg") is None


def test_media_binary_respects_application_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_REC_LAB_FFMPEG_BIN", str(tmp_path))
    assert installed_ffmpeg_dir() == tmp_path


def test_tiktok_denial_is_not_a_fallback_candidate():
    status = recording_exit_kind(stderr="HTTP 403 Forbidden", returncode=1, stopped=False)
    assert status == "restricted"
    assert reconnect_action(kind=status, bytes_written=0, engine_index=0, engine_count=2) == "stop"


def test_ffmpeg_command_opens_twitcasting_like_the_player():
    url = "https://edge.twitcasting.tv/live.m3u8"
    command = build_command("ffmpeg", url, Path("/tmp/out.ts"), "best")
    assert command[0].endswith("ffmpeg")
    assert command[command.index("-icy") + 1] == "0"
    assert command.index("-icy") < command.index("-i")
    assert command.index("-user_agent") < command.index("-i")
    assert command.index("-headers") < command.index("-i")
    assert command[command.index("-headers") + 1] == "Referer: https://twitcasting.tv/\r\n"
    assert command[command.index("-i") + 1] == url
    assert "-c" in command and "copy" in command
    assert isinstance(command, list)


_PLAYLIST = "https://203-0-113-10.twitcasting.tv/tc.livehls/v1/streams/819154156/hls/672.96/media.m3u8"
_PREROLL_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:9
#EXT-X-MEDIA-SEQUENCE:2
#EXT-X-MAP:URI="/tc.livehls/v1/streams/819154156/hls/preroll-init.2.mp4"
#EXTINF:2.000,
/tc.livehls/v1/streams/819154156/hls/preroll-media.2.mp4
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="/tc.livehls/v1/streams/819154156/hls/672.96/init.3.mp4"
#EXTINF:8.332,
/tc.livehls/v1/streams/819154156/hls/672.96/media.0.mp4
#EXTINF:8.334,
https://evil.example/media.1.mp4
"""


def test_preroll_segments_are_removed_and_other_hosts_dropped():
    filtered = filter_preroll_playlist(_PREROLL_PLAYLIST, _PLAYLIST)
    assert filtered.dropped_preroll == 1
    assert filtered.kept_segments == 1
    assert "preroll-" not in filtered.text
    assert "#EXT-X-DISCONTINUITY" not in filtered.text
    assert "evil.example" not in filtered.text
    assert "#EXT-X-MEDIA-SEQUENCE:3" in filtered.text
    assert "https://203-0-113-10.twitcasting.tv/tc.livehls/v1/streams/819154156/hls/672.96/media.0.mp4" in filtered.text
    assert "https://203-0-113-10.twitcasting.tv/tc.livehls/v1/streams/819154156/hls/672.96/init.3.mp4" in filtered.text


_UNNAMED_PREFIX = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA-SEQUENCE:4
#EXTINF:6.000,
/hls/intro.mp4
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="/hls/init.mp4"
#EXTINF:8.000,
/hls/media.0.mp4
#EXT-X-DISCONTINUITY
#EXTINF:8.000,
/hls/media.1.mp4
"""


def test_unnamed_prefix_before_first_discontinuity_is_kept():
    filtered = filter_preroll_playlist(_UNNAMED_PREFIX, "https://edge.twitcasting.tv/hls/media.m3u8")
    assert filtered.removed is False
    assert filtered.dropped_preroll == 0
    assert filtered.kept_segments == 3
    assert "intro.mp4" in filtered.text
    assert filtered.text.count("#EXT-X-DISCONTINUITY") == 2
    assert "#EXT-X-MEDIA-SEQUENCE:4" in filtered.text
    assert "https://edge.twitcasting.tv/hls/init.mp4" in filtered.text
    assert filtered.text.index("intro.mp4") < filtered.text.index("#EXT-X-DISCONTINUITY")

def test_playlist_without_preroll_or_discontinuity_is_unchanged():
    playlist = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:5
#EXTINF:8.0,
https://edge.twitcasting.tv/a/media.0.mp4
#EXTINF:8.0,
https://edge.twitcasting.tv/a/media.1.mp4
"""
    filtered = filter_preroll_playlist(playlist, "https://edge.twitcasting.tv/a/media.m3u8")
    assert filtered.removed is False
    assert filtered.dropped_preroll == 0
    assert filtered.kept_segments == 2


def test_variant_skips_other_hosts():
    playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000
https://edge.twitcasting.tv/low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000
https://evil.example/high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000
https://edge.twitcasting.tv/mid.m3u8
"""
    assert pick_variant(playlist, "https://twitcasting.tv/master.m3u8", "best") == "https://edge.twitcasting.tv/mid.m3u8"
    assert pick_variant(playlist, "https://twitcasting.tv/master.m3u8", "worst") == "https://edge.twitcasting.tv/low.m3u8"


def test_mirror_serves_playlist_without_preroll():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["referer"] == "https://twitcasting.tv/"
        return httpx.Response(200, text=_PREROLL_PLAYLIST)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            mirror = HlsMirror(_PLAYLIST, client=client)
            try:
                local = await mirror.ffmpeg_input(asyncio.Event())
                assert local is not None
                assert local.startswith("http://127.0.0.1:")
                async with httpx.AsyncClient() as reader:
                    served = await reader.get(local)
                assert served.status_code == 200
                assert "preroll-" not in served.text
                assert "evil.example" not in served.text
                assert len([line for line in served.text.splitlines() if line.startswith("/")]) == 1
            finally:
                await mirror.aclose()

    asyncio.run(run())


def test_mirror_serves_local_playlist_with_unclassified_prefix_intact():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_UNNAMED_PREFIX)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            mirror = HlsMirror("https://edge.twitcasting.tv/hls/media.m3u8", client=client)
            try:
                local = await mirror.ffmpeg_input(asyncio.Event())
                assert local is not None
                assert local.startswith("http://127.0.0.1:")
                assert local != mirror.upstream
                async with httpx.AsyncClient() as reader:
                    served = await reader.get(local)
                assert served.text.count("#EXT-X-DISCONTINUITY") == 2
                assert len([line for line in served.text.splitlines() if line.startswith("/")]) == 3
            finally:
                await mirror.aclose()

    asyncio.run(run())


def test_mirror_waits_while_only_preroll_candidate_is_available():
    short = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:1
#EXTINF:6.000,
https://edge.twitcasting.tv/hls/preroll-candidate.mp4
"""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, text=short if calls["count"] == 1 else _PREROLL_PLAYLIST)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            mirror = HlsMirror("https://edge.twitcasting.tv/hls/media.m3u8", client=client, wait_seconds=3)
            try:
                local = await mirror.ffmpeg_input(asyncio.Event())
                assert local is not None
                assert local.startswith("http://127.0.0.1:")
                async with httpx.AsyncClient() as reader:
                    served = await reader.get(local)
                assert calls["count"] >= 2
                assert "#EXT-X-MEDIA-SEQUENCE:3" in served.text
            finally:
                await mirror.aclose()

    asyncio.run(run())


def test_mirror_keeps_segment_redirects_behind_validated_local_endpoint():
    playlist = """#EXTM3U
#EXTINF:8.0,
https://edge.twitcasting.tv/a/media.0.mp4
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=playlist)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            upstream = "https://edge.twitcasting.tv/a/media.m3u8"
            mirror = HlsMirror(upstream, client=client)
            try:
                chosen = await mirror.ffmpeg_input(asyncio.Event())
                assert chosen is not None and chosen.startswith("http://127.0.0.1:")
            finally:
                await mirror.aclose()

    asyncio.run(run())


def test_retry_after_valid_segment_is_partial(db):
    now = utcnow()
    streamer = Streamer(
        platform="twitcasting",
        platform_user_id="caster",
        display_name="caster",
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=True,
        live_status="LIVE",
        created_at=now,
        updated_at=now,
    )
    db.add(streamer)
    db.flush()
    session = StreamSession(
        streamer_id=streamer.id,
        platform_stream_id="1",
        detected_at=now,
        status="ENDED",
        manual_stop=False,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    job = RecordingJob(
        stream_session_id=session.id,
        status="FINALIZING",
        retry_count=5,
        manual_stop_requested=False,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.commit()
    _finish(job.id, any_valid=True, manual=False, failed=True)
    db.refresh(job)
    assert job.status == "PARTIAL"


def test_grace_ends_session_while_recorder_is_alive(db):
    from live_rec_lab.services.sessions import register_streamer

    streamer = register_streamer(
        db,
        platform="twitcasting",
        account="caster",
        display_name="配信者",
        monitoring=True,
        recording=True,
        comments=True,
    )
    now = utcnow()
    apply_snapshot(
        db,
        streamer,
        Snapshot("LIVE", platform_stream_id="movie-1"),
        now,
        grace=30,
        recorder_alive=lambda _session_id: True,
    )
    apply_snapshot(
        db,
        streamer,
        Snapshot("OFFLINE"),
        now + timedelta(seconds=10),
        grace=30,
        recorder_alive=lambda _session_id: True,
    )
    ended = apply_snapshot(
        db,
        streamer,
        Snapshot("OFFLINE"),
        now + timedelta(seconds=40),
        grace=30,
        recorder_alive=lambda _session_id: True,
    )
    assert session_status(db) == "ENDED"
    assert any(item.kind == "stop_recording" for item in ended)


def session_status(db) -> str:
    from sqlalchemy import select

    return db.scalars(select(StreamSession)).one().status


def test_streamlink_command_has_no_shell_metacharacters():
    command = build_command("streamlink", "https://www.tiktok.com/@abc/live", Path("/tmp/out.ts"), "best")
    assert command[0].endswith("streamlink")
    assert "--output" in command
    assert "best" in command
    assert not any("|" in part or ";" in part for part in command)


def test_ytdlp_writes_hls_as_mpegts_without_cookies():
    command = build_command("yt-dlp", "https://www.tiktok.com/@abc/live", Path("/tmp/out.ts"), "best")
    assert command.index("--hls-use-mpegts") < command.index("-o")
    assert "--cookies" not in command
    assert not any("captcha" in part.lower() for part in command)


def test_retry_does_not_warn_about_returning_to_streamlink():
    assert engine_switch_notice("yt-dlp", "streamlink", False) is None
    assert recording_engines("tiktok", None) == ["streamlink", "yt-dlp"]
    assert recording_engines("tiktok", "yt-dlp") == ["yt-dlp", "streamlink"]
    assert recording_engines("tiktok", "streamlink") == ["streamlink", "yt-dlp"]


def test_recording_exit_retries_the_same_engine_without_ending():
    offline = recording_exit_kind(stderr="error: user is offline", returncode=1, stopped=False)
    assert offline == "offline"
    assert reconnect_action(
        kind="offline",
        bytes_written=1024,
        engine_index=0,
        engine_count=2,
    ) == "stop"
    assert recording_exit_kind(stderr="", returncode=0, stopped=False) == "ended"
    assert reconnect_action(
        kind="ended",
        bytes_written=1024,
        engine_index=1,
        engine_count=2,
    ) == "stop"
    assert recording_exit_kind(stderr="read timeout", returncode=1, stopped=False) == "interrupted"
    same = reconnect_action(
        kind="interrupted",
        bytes_written=1024,
        engine_index=0,
        engine_count=2,
    )
    switched = reconnect_action(kind="interrupted", bytes_written=0, engine_index=0, engine_count=2)
    waiting = reconnect_action(
        kind="offline",
        bytes_written=0,
        engine_index=1,
        engine_count=2,
        session_live=True,
    )
    ended_empty = reconnect_action(
        kind="ended",
        bytes_written=0,
        engine_index=1,
        engine_count=2,
        session_live=True,
    )
    assert same == "same"
    assert switched == "switch"
    assert waiting == "wait"
    assert ended_empty == "wait"
    assert reconnect_action(
        kind="ended",
        bytes_written=1024,
        engine_index=0,
        engine_count=2,
        session_live=True,
    ) == "stop"
    assert reconnect_action(
        kind="offline",
        bytes_written=0,
        engine_index=1,
        engine_count=2,
        session_live=False,
    ) == "stop"


def test_streamlink_fallback_is_recorded_once():
    first = engine_switch_notice("streamlink", "yt-dlp", False)
    assert first == "録画エンジンを streamlink から yt-dlp に切り替えました"
    assert engine_switch_notice("streamlink", "yt-dlp", True) is None


def test_repeated_monitor_detail_is_one_notice():
    details = MonitorDetails()
    message = "配信状態を確認できません"
    assert details.notice(2, "UNKNOWN", message) == message
    assert details.notice(2, "UNKNOWN", message) is None
    assert details.notice(2, "LIVE", None) is None
    assert details.notice(2, "UNKNOWN", message) == message
    assert details.notice(2, "UNKNOWN", "配信 ID を取得できません") == "配信 ID を取得できません"


def test_hevc_remux_uses_hvc1_and_h264_does_not(tmp_path):
    source = tmp_path / "in.ts"
    dest = tmp_path / "out.mp4"
    hevc = remux_args("ffmpeg", source, dest, "hevc")
    h264 = remux_args("ffmpeg", source, dest, "h264")
    assert hevc[hevc.index("-tag:v") + 1] == "hvc1"
    assert "-tag:v" not in h264


def test_unreadable_codec_is_distinct_from_an_empty_file():
    assert describe_probe_failure("", 0) == "録画ファイルが空です"
    assert describe_probe_failure("Video codec (c) is not implemented", 49_938_432) == "映像コーデックを読めません"


def test_empty_file_is_not_valid(tmp_path):
    temp = tmp_path / "empty.part.ts"
    temp.write_bytes(b"")
    segment = RecordingSegment(
        recording_job_id=1,
        segment_number=1,
        started_at=utcnow(),
        temp_path=str(temp),
        file_path=str(tmp_path / "out.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=utcnow(),
    )
    reason = finalize_segment(segment)
    assert segment.validation_status == "invalid"
    assert reason == "録画ファイルが空です"


def _ffprobe_result(payload: dict) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["ffprobe"],
        returncode=0,
        stdout=json.dumps(payload).encode(),
        stderr=b"",
    )


def test_video_without_codec_name_is_not_a_successful_probe(tmp_path, monkeypatch):
    media = tmp_path / "live.flv"
    media.write_bytes(b"FLV\x01\x05")
    payload = {
        "format": {"duration": "73.401", "format_name": "flv"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "width": 1080, "height": 1920},
        ],
    }
    monkeypatch.setattr(
        "live_rec_lab.recording.segments.subprocess.run",
        lambda *args, **kwargs: _ffprobe_result(payload),
    )
    info = probe_media(media)
    assert info is not None
    assert info.unnamed_video
    assert info.video_codec is None
    assert info.duration_ms == 73401


def test_audio_only_probe_is_not_unnamed_video(tmp_path, monkeypatch):
    media = tmp_path / "voice.flv"
    media.write_bytes(b"FLV\x01\x04")
    payload = {
        "format": {"duration": "8.0", "format_name": "flv"},
        "streams": [{"codec_type": "audio", "codec_name": "aac"}],
    }
    monkeypatch.setattr(
        "live_rec_lab.recording.segments.subprocess.run",
        lambda *args, **kwargs: _ffprobe_result(payload),
    )
    info = probe_media(media)
    assert info is not None
    assert not info.unnamed_video
    assert info.video_codec is None


def test_unnamed_video_file_stays_invalid(tmp_path, monkeypatch):
    temp = tmp_path / "live.part.ts"
    temp.write_bytes(b"FLV\x01\x05" + b"\x00" * 32)
    dest = tmp_path / "live.mp4"
    payload = {
        "format": {"duration": "73.401", "format_name": "flv"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video"},
        ],
    }
    monkeypatch.setattr(
        "live_rec_lab.recording.segments.subprocess.run",
        lambda *args, **kwargs: _ffprobe_result(payload),
    )
    segment = RecordingSegment(
        recording_job_id=1,
        segment_number=1,
        started_at=utcnow(),
        temp_path=str(temp),
        file_path=str(dest),
        validation_status="pending",
        srt_status="not_generated",
        created_at=utcnow(),
    )
    reason = finalize_segment(segment)
    assert reason == "映像コーデックを読めません"
    assert segment.validation_status == "invalid"
    assert segment.file_size == temp.stat().st_size
    assert temp.is_file()
    assert not dest.exists()
    assert not dest.with_suffix(".ts").exists()


def test_nonempty_unreadable_file_stays_invalid(tmp_path):
    temp = tmp_path / "broken.part.ts"
    temp.write_bytes(b"FLV\x01\x05" + b"\x00" * 64)
    segment = RecordingSegment(
        recording_job_id=1,
        segment_number=1,
        started_at=utcnow(),
        temp_path=str(temp),
        file_path=str(tmp_path / "out.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=utcnow(),
    )
    reason = finalize_segment(segment)
    assert segment.validation_status == "invalid"
    assert reason == "録画ファイルを検証できません"
    assert segment.file_size == temp.stat().st_size


def test_local_media_is_finalized(tmp_path):
    source = tmp_path / "clip.part.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000",
            "-t",
            "0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-f",
            "mpegts",
            str(source),
        ],
        check=True,
    )
    segment = RecordingSegment(
        recording_job_id=1,
        segment_number=1,
        started_at=utcnow(),
        temp_path=str(source),
        file_path=str(tmp_path / "clip.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=utcnow(),
    )
    finalize_segment(segment)
    assert segment.validation_status == "valid"
    kept = probe_media(Path(segment.file_path))
    assert kept is not None
    assert segment.duration_ms == kept.duration_ms
    assert Path(segment.file_path).is_file()
    assert segment.container_format == "mp4"


def test_duration_uses_the_kept_file_not_the_source(tmp_path, monkeypatch):
    source = tmp_path / "in.part.ts"
    source.write_bytes(b"source-bytes")
    dest = tmp_path / "out.mp4"

    def fake_probe(path: Path) -> Probe:
        if path == dest:
            return Probe(35_966, "mp4", 0.0, "h264", audio_codec="aac")
        format_name = "mpegts" if path == source else "mp4"
        return Probe(77_933, format_name, 1.4, "h264", audio_codec="aac")

    def fake_remux(src: Path, target: Path, video_codec: str | None = None) -> bool:
        del src, video_codec
        target.write_bytes(b"mp4-bytes")
        return True

    monkeypatch.setattr("live_rec_lab.recording.segments.probe_media", fake_probe)
    monkeypatch.setattr("live_rec_lab.recording.segments.remux_mp4", fake_remux)
    segment = RecordingSegment(
        recording_job_id=1,
        segment_number=1,
        started_at=utcnow(),
        temp_path=str(source),
        file_path=str(dest),
        validation_status="pending",
        srt_status="not_generated",
        created_at=utcnow(),
    )
    assert finalize_segment(segment) is None
    assert segment.validation_status == "valid"
    assert segment.duration_ms == 35_966
    assert not source.exists()


def test_finalization_preserves_a_competing_output(tmp_path, monkeypatch):
    source = tmp_path / "attempt.ts"
    source.write_bytes(b"source")
    target = tmp_path / "movie.mp4"

    def remux(_source, output, _codec=None):
        output.write_bytes(b"ours")
        target.write_bytes(b"unrelated")
        return True

    monkeypatch.setattr("live_rec_lab.recording.segments.remux_mp4", remux)
    monkeypatch.setattr(
        "live_rec_lab.recording.segments.probe_media",
        lambda path: Probe(400, "mpegts" if path == source else "mp4", 0.0, "h264", audio_codec="aac"),
    )
    segment = RecordingSegment(
        recording_job_id=1, segment_number=1, started_at=utcnow(), temp_path=str(source), reserved_path=str(target),
        validation_status="pending",
    )
    error = finalize_segment(segment)

    assert error
    assert segment.validation_status == "pending"
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"unrelated"


def test_kept_segment_numbers_continue_across_jobs(db, tmp_path):
    session, job = _recording_job(db, status="LIVE")
    broadcast = session.detected_at
    segment_started = broadcast + timedelta(minutes=3)
    db.commit()
    assert session_filename_time(session.id) == broadcast
    assert broadcast_filename_time(None, broadcast) == broadcast
    assert _initial_segment_number(session.id) == 1
    number = next_segment_number(1, kept=False)
    assert number == 1
    _folder, _temp, final = recording_paths(tmp_path, "tiktok", "recorder", broadcast, "Asia/Tokyo", number)
    assert final.name.endswith("_s001.mp4")
    db.add(
        RecordingSegment(
            recording_job_id=job.id,
            stream_session_id=session.id,
            segment_number=number,
            started_at=segment_started,
            file_path=str(final),
            validation_status="valid",
            srt_status="not_generated",
            created_at=segment_started,
        )
    )
    db.commit()
    assert _initial_segment_number(session.id) == 2
    _folder, _temp, second = recording_paths(tmp_path, "tiktok", "recorder", broadcast, "Asia/Tokyo", 2)
    assert second.name.endswith("_s002.mp4")
    assert final.name.removesuffix("_s001.mp4") == second.name.removesuffix("_s002.mp4")


def test_reserve_numbers_atomically_across_jobs(db, tmp_path):
    stream, first_job = _recording_job(db, status="LIVE")
    second_job = RecordingJob(stream_session_id=stream.id, status="STARTING")
    db.add(second_job)
    db.commit()
    session_id = stream.id
    attempts = [
        _open_attempt(job.id, session_id, tmp_path, "tiktok", "recorder", stream.detected_at, "Asia/Tokyo")
        for job in (first_job, second_job)
    ]
    for _, path in attempts:
        path.write_bytes(b"captured media")

    def reserve(segment_id):
        with session_scope(write_lock=True) as transaction:
            segment = transaction.get(RecordingSegment, segment_id)
            reserve_segment_path(transaction, segment, tmp_path)
            return segment.segment_number, Path(segment.reserved_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = dict(executor.map(reserve, [segment_id for segment_id, _ in attempts]))

    assert set(results) == {1, 2}
    assert results[1].name.endswith("_s001.mp4")
    assert results[2].name.endswith("_s002.mp4")
    with session_scope() as transaction:
        assert transaction.get(StreamSession, session_id).next_segment_number == 3


def test_first_windows_filename_conflict_uses_fixed_session_suffix(db, tmp_path):
    stream, job = _recording_job(db, status="LIVE")
    db.commit()
    first, first_path = _open_attempt(
        job.id, stream.id, tmp_path, "tiktok", "recorder", stream.detected_at, "Asia/Tokyo",
    )
    first_path.write_bytes(b"captured media")
    local = stream.detected_at.astimezone(ZoneInfo("Asia/Tokyo"))
    conflict = first_path.parent / f"TIKTOK_RECORDER_{local:%Y%m%d_%H%M%S}_S001.SRT"
    conflict.write_bytes(b"old subtitles")
    second, second_path = _open_attempt(
        job.id, stream.id, tmp_path, "tiktok", "recorder", stream.detected_at, "Asia/Tokyo",
    )
    second_path.write_bytes(b"captured media")

    with session_scope(write_lock=True) as transaction:
        rows = [transaction.get(RecordingSegment, segment_id) for segment_id in (first, second)]
        for row in rows:
            reserve_segment_path(transaction, row, tmp_path)
            transaction.flush()
        first_name, second_name = [Path(row.reserved_path).name for row in rows]

    assert "_q" in first_name
    assert first_name.removesuffix("001.mp4") == second_name.removesuffix("002.mp4")
    assert conflict.read_bytes() == b"old subtitles"


def test_filename_clock_stays_on_the_first_broadcast_time(db, tmp_path):
    session, _job = _recording_job(db, status="LIVE")
    detected = session.detected_at
    platform_started = detected - timedelta(minutes=5)
    db.commit()
    assert session_filename_time(session.id) == detected
    manager = RecordingManager()
    assert manager.filename_clock(session.id) == detected
    with session_scope() as scoped:
        row = scoped.get(StreamSession, session.id)
        assert row is not None
        row.platform_started_at = platform_started
    assert manager.filename_clock(session.id) == detected
    assert session_filename_time(session.id) == platform_started
    _folder, _temp, first = recording_paths(tmp_path, "tiktok", "recorder", detected, "Asia/Tokyo", 1)
    _folder, _temp, second = recording_paths(tmp_path, "tiktok", "recorder", detected, "Asia/Tokyo", 2)
    assert first.parent == second.parent
    assert first.name.endswith("_s001.mp4")
    assert second.name.endswith("_s002.mp4")
    assert first.name.replace("_s001", "") == second.name.replace("_s002", "")


def test_empty_attempt_does_not_force_partial(db, tmp_path):
    empty_path = tmp_path / "empty.part.ts"
    empty_path.write_bytes(b"")
    now = utcnow()
    empty = RecordingSegment(
        recording_job_id=1,
        segment_number=1,
        started_at=now,
        temp_path=str(empty_path),
        file_path=str(tmp_path / "empty.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    finalize_segment(empty)
    assert not counts_as_recording_failure(empty)
    broken_path = tmp_path / "broken.part.ts"
    broken_path.write_bytes(b"not-media")
    broken = RecordingSegment(
        recording_job_id=1,
        segment_number=2,
        started_at=now,
        temp_path=str(broken_path),
        file_path=str(tmp_path / "broken.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    finalize_segment(broken)
    assert counts_as_recording_failure(broken)
    session, job = _recording_job(db, status="ENDED")
    db.commit()
    _finish(job.id, any_valid=True, manual=False, failed=counts_as_recording_failure(empty))
    db.refresh(job)
    assert job.status == "COMPLETED"
    later = utcnow()
    other = RecordingJob(
        stream_session_id=session.id,
        status="FINALIZING",
        retry_count=0,
        manual_stop_requested=False,
        started_at=later,
        created_at=later,
        updated_at=later,
    )
    db.add(other)
    db.commit()
    _finish(other.id, any_valid=True, manual=False, failed=counts_as_recording_failure(broken))
    db.refresh(other)
    assert other.status == "PARTIAL"


def test_ending_keeps_recording_and_ended_stops():
    assert recording_continues("LIVE", stopped=False)
    assert recording_continues("ENDING", stopped=False)
    assert not recording_continues("ENDED", stopped=False)
    assert not recording_continues("LIVE", stopped=True)
    assert not recording_continues(None, stopped=False)


def _recording_job(db, *, status: str, manual: bool = False):
    now = utcnow()
    streamer = Streamer(
        platform="tiktok",
        platform_user_id="recorder",
        display_name="recorder",
        monitoring_enabled=True,
        recording_enabled=True,
        comments_enabled=False,
        live_status="LIVE",
        created_at=now,
        updated_at=now,
    )
    db.add(streamer)
    db.flush()
    session = StreamSession(
        streamer_id=streamer.id,
        platform_stream_id="room",
        detected_at=now,
        status=status,
        manual_stop=manual,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    job = RecordingJob(
        stream_session_id=session.id,
        status="RECORDING",
        retry_count=0,
        manual_stop_requested=manual,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    return session, job


def test_retry_cap_message_is_only_after_five_failures(db):
    session, job = _recording_job(db, status="LIVE")
    db.commit()
    assert _schedule_retry(job.id, session.id, 1) is True
    db.expire_all()
    reasons = list(
        db.scalars(select(CollectionGap.reason).where(CollectionGap.stream_session_id == session.id))
    )
    assert reasons[-1] == "録画が中断したため次のファイルで再開します"
    assert _schedule_retry(job.id, session.id, 5) is False
    db.expire_all()
    reasons = list(
        db.scalars(select(CollectionGap.reason).where(CollectionGap.stream_session_id == session.id))
    )
    assert reasons[-1] == "再接続の上限に達しました"


def test_empty_attempt_is_removed_and_unreadable_file_stays(db, tmp_path):
    stream, job = _recording_job(db, status="LIVE")
    now = utcnow()
    empty = tmp_path / "empty.part.ts"
    empty.write_bytes(b"")
    empty_segment = RecordingSegment(
        recording_job_id=job.id,
        stream_session_id=stream.id,
        segment_number=1,
        started_at=now,
        temp_path=str(empty),
        file_path=str(tmp_path / "empty.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    broken = tmp_path / "broken.part.ts"
    broken.write_bytes(b"not-media")
    broken_segment = RecordingSegment(
        recording_job_id=job.id,
        stream_session_id=stream.id,
        segment_number=2,
        started_at=now,
        temp_path=str(broken),
        file_path=str(tmp_path / "broken.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    db.add(empty_segment)
    db.add(broken_segment)
    db.flush()
    empty_id = empty_segment.id
    finalize_segment(empty_segment)
    assert attempt_is_empty(empty_segment)
    unlink_empty_files(empty_segment)
    db.delete(empty_segment)
    finalize_segment(broken_segment)
    assert not attempt_is_empty(broken_segment)
    db.flush()
    assert db.get(RecordingSegment, empty_id) is None
    assert broken_segment.validation_status == "invalid"
    assert broken.is_file()


def test_cancel_finalizes_open_segment_without_marking_manual(db, tmp_path):
    session, job = _recording_job(db, status="ENDING")
    source = tmp_path / "open.part.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000",
            "-t",
            "0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-f",
            "mpegts",
            str(source),
        ],
        check=True,
    )
    now = utcnow()
    segment = RecordingSegment(
        recording_job_id=job.id,
        stream_session_id=session.id,
        segment_number=1,
        started_at=now,
        temp_path=str(source),
        reserved_path=str(tmp_path / "open.mp4"),
        validation_status="pending",
        srt_status="not_generated",
        created_at=now,
    )
    db.add(segment)
    db.commit()
    outcome = _finalize_open(segment.id, session.id, tmp_path)
    assert outcome is True
    manual = _manual(job.id)
    _finish(job.id, any_valid=True, manual=manual, failed=False)
    db.refresh(job)
    db.refresh(segment)
    assert manual is False
    assert job.status == "COMPLETED"
    assert job.status != "CANCELLED"
    assert segment.validation_status == "valid"
    assert Path(segment.file_path).is_file()


def test_cancelled_command_reaps_the_child():
    async def run() -> None:
        seen: list[int] = []

        def watch(process: asyncio.subprocess.Process) -> None:
            seen.append(process.pid)

        task = asyncio.create_task(run_command(["sleep", "30"], asyncio.Event(), on_process=watch))
        for _ in range(40):
            if seen:
                break
            await asyncio.sleep(0.05)
        assert seen
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(seen[0], 0)

    asyncio.run(run())


def test_stop_timeout_kills_the_child_before_cancel(monkeypatch):
    async def immediate_timeout(awaitable, timeout=8):
        del awaitable, timeout
        raise TimeoutError

    monkeypatch.setattr("live_rec_lab.recording.manager.asyncio.wait_for", immediate_timeout)

    async def run() -> None:
        manager = RecordingManager()
        started = asyncio.Event()
        observed: dict[str, object] = {}

        async def stuck() -> None:
            process = await asyncio.create_subprocess_exec(
                "sleep",
                "30",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            manager.processes[7] = process
            observed["pid"] = process.pid
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                try:
                    os.kill(process.pid, 0)
                    observed["alive"] = True
                except ProcessLookupError:
                    observed["alive"] = False
                raise

        manager.stops[7] = asyncio.Event()
        manager.tasks[7] = asyncio.create_task(stuck())
        await started.wait()
        await manager.stop(7, manual=False)
        assert observed["alive"] is False

    asyncio.run(run())
