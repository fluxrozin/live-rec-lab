"""TC-024〜028、TC-032。外部サービスの代わりにローカル映像とコメントを使う。"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import (
    Comment,
    CommentCollectionJob,
    RecordingJob,
    RecordingSegment,
    StreamSession,
    SystemEvent,
)
from live_rec_lab.db.queries import load_settings, save_settings
from live_rec_lab.db.session import session_scope
from live_rec_lab.platforms.base import CommentIn, Snapshot
from live_rec_lab.platforms.tiktok import TikTokAdapter
from live_rec_lab.services.jobs import Coordinator
from live_rec_lab.services.sessions import register_streamer


class LocalTikTok(TikTokAdapter):
    status = "LIVE"

    def __init__(self):
        self.pending = asyncio.Queue()

    async def check(self, account):
        return Snapshot(self.status, "4096" if self.status == "LIVE" else None)

    async def stream_comments(self, account, stream_id, stop, *, on_connect):
        assert stream_id == "4096"
        on_connect()
        stopper = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                item = asyncio.create_task(self.pending.get())
                await asyncio.wait((stopper, item), return_when=asyncio.FIRST_COMPLETED)
                if item.done():
                    yield item.result()
                else:
                    item.cancel()
                    await asyncio.gather(item, return_exceptions=True)
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)


async def eventually(condition):
    async with asyncio.timeout(5):
        while not (result := condition()):
            await asyncio.sleep(0.025)
    return result


@pytest.fixture
def local_hls(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg / ffprobe unavailable")
    from live_rec_lab import storage
    from live_rec_lab.recording import manager

    source = tmp_path / "source.m3u8"
    subprocess.run(
        [
            ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=10",
            "-f", "lavfi", "-i", "sine=frequency=600", "-t", "6", "-c:v", "libx264", "-threads", "1",
            "-c:a", "aac", "-g", "10", "-hls_time", "1", "-hls_list_size", "0", "-f", "hls", str(source),
        ], check=True, timeout=10,
    )
    # Feed a local HLS playlist into the real capture subprocess instead of connecting to the service.
    monkeypatch.setattr(
        manager, "build_command",
        lambda engine, url, output, quality: [
            ffmpeg, "-nostdin", "-loglevel", "error", "-re", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-flush_packets", "1", "-f", "mpegts", str(output),
        ],
    )
    monkeypatch.setenv("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", "none")
    physical_status = storage.recording_root_status
    monkeypatch.setattr(
        storage, "recording_root_status", lambda path, **_: physical_status(path, require_windows=False),
    )


def test_tiktok_detection_capture_comment_srt_and_switches(db, paths, local_hls):
    streamer = register_streamer(
        db, platform="tiktok", account="example", display_name="配信者",
        monitoring=True, recording=True, comments=True,
    )
    streamer_id = streamer.id
    save_settings(db, {"recording_root": str(paths["recordings"]), "min_free_bytes": 0})
    db.commit()

    def first_media():
        with session_scope() as conn:
            return conn.scalar(select(RecordingSegment.first_media_at).where(
                RecordingSegment.first_media_at.is_not(None),
            ))

    def comment_count():
        with session_scope() as conn:
            return conn.scalar(select(func.count()).select_from(Comment))

    async def run():
        platform = LocalTikTok()
        coordinator = Coordinator(platform)

        async def check():
            with session_scope() as conn:
                settings = load_settings(conn)
            await coordinator.check_streamer(streamer_id, "tiktok", "example", 0, settings)

        try:
            await check()
            await eventually(first_media)
            await platform.pending.put(CommentIn(
                message="テスト配信🙂", platform_comment_id="42", posted_at=utcnow(), source="test",
            ))
            await eventually(comment_count)
            await asyncio.sleep(1)

            with session_scope() as conn:
                stream_id = conn.scalars(select(StreamSession.id)).one()
            assert await coordinator.manual_stop(stream_id)
            assert coordinator.comments_alive(stream_id)
            await check()
            assert not coordinator.recorder.alive(stream_id)

            platform.status = "OFFLINE"
            await check()
            assert coordinator.comments_alive(stream_id)
            await check()
            assert not coordinator.comments_alive(stream_id)
        finally:
            await coordinator.shutdown()

    asyncio.run(run())

    with session_scope() as conn:
        stream = conn.scalars(select(StreamSession)).one()
        recording = conn.scalars(select(RecordingJob)).one()
        segment = conn.scalars(select(RecordingSegment)).one()
        comments = conn.scalars(select(CommentCollectionJob)).one()
        assert stream.status == "ENDED" and stream.end_reason == "platform_end"
        assert recording.end_reason == "manual_stop"
        assert comments.status == "COMPLETED" and comments.stored_count == 1
        assert segment.validation_status == "valid" and segment.segment_number == 1
        video = Path(segment.file_path)
        subtitle = video.with_suffix(".srt")
        assert video.is_file() and subtitle.is_file()
        assert "テスト配信🙂" in subtitle.read_text(encoding="utf-8")
        assert not segment.srt_provisional

def test_parallel_recording_respects_the_persisted_limit(db, paths, local_hls):
    streamers = [
        register_streamer(
            db, platform="tiktok", account=account, display_name=account,
            monitoring=True, recording=True, comments=False,
        )
        for account in ("one", "two", "three")
    ]
    accounts = [(item.id, item.platform_user_id) for item in streamers]
    save_settings(db, {
        "recording_root": str(paths["recordings"]), "min_free_bytes": 0, "max_parallel_recordings": 2,
    })
    db.commit()

    def started_count():
        with session_scope() as conn:
            return conn.scalar(select(func.count(RecordingSegment.id)).where(
                RecordingSegment.first_media_at.is_not(None),
            )) == 2

    async def run():
        coordinator = Coordinator(LocalTikTok())
        with session_scope() as conn:
            settings = load_settings(conn)
        try:
            for streamer_id, account in accounts:
                await coordinator.check_streamer(streamer_id, "tiktok", account, 0, settings)
            await eventually(started_count)
            assert sum(not task.done() for task in coordinator.recorder.tasks.values()) == 2
        finally:
            await coordinator.shutdown()

    asyncio.run(run())
    with session_scope() as conn:
        assert conn.scalar(select(func.count(RecordingJob.id))) == 2
        assert conn.scalar(select(func.count(SystemEvent.id)).where(SystemEvent.event_type == "recording_delayed")) == 1
        segments = list(conn.scalars(select(RecordingSegment).where(RecordingSegment.validation_status == "valid")))
        assert len(segments) == 2
        assert len({segment.stream_session_id for segment in segments}) == 2
        assert all(Path(segment.file_path).is_file() for segment in segments)
