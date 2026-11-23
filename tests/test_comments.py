import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from live_rec_lab.comments.store import store_comment
from live_rec_lab.config import utcnow
from live_rec_lab.db.models import Comment, CommentCollectionJob
from live_rec_lab.db.queries import collection_label
from live_rec_lab.platforms.base import CommentIn, Snapshot
from live_rec_lab.platforms.tiktok import CommentConnectionError, TikTokAdapter
from live_rec_lab.services.sessions import apply_snapshot, register_streamer


class MockTikTokClient:
    def __init__(self, receive):
        self.receive = receive
        self.handlers = {}
        self.receiver = None
        self.disconnected = False
        self.web_closed = False
        self.web = SimpleNamespace(close=self.close_web)

    def on(self, event):
        def register(handler):
            self.handlers[event.__name__] = handler
            return handler

        return register

    async def start(self, *, room_id, fetch_live_check):
        assert room_id == 102
        assert fetch_live_check is False

        async def run_receiver():
            self.handlers["ConnectEvent"](SimpleNamespace(room_id=room_id))
            await self.receive(self.handlers["CommentEvent"])

        self.receiver = asyncio.create_task(run_receiver())
        return self.receiver

    async def disconnect(self):
        self.disconnected = True
        if self.receiver is not None:
            try:
                await self.receiver
            except Exception:
                pass

    async def close_web(self):
        self.web_closed = True


def test_tiktok_comment_stream_waits_for_receiver_and_drains_queue(monkeypatch):
    async def receive(handler):
        await asyncio.sleep(0.25)
        handler(SimpleNamespace(content="先に到着"))
        handler(SimpleNamespace(content="続けて到着"))

    client = MockTikTokClient(receive)
    monkeypatch.setattr("TikTokLive.TikTokLiveClient", lambda **_: client)

    async def collect():
        async with asyncio.timeout(2):
            comments = TikTokAdapter().stream_comments("alpha", "102", asyncio.Event(), on_connect=lambda: None)
            return [item.message async for item in comments]

    assert asyncio.run(collect()) == ["先に到着", "続けて到着"]
    assert client.disconnected and client.web_closed


def test_tiktok_comment_stream_propagates_receiver_error_after_buffered_comments(monkeypatch):
    async def receive(handler):
        handler(SimpleNamespace(content="切断前のコメント"))
        raise RuntimeError("receiver failed")

    client = MockTikTokClient(receive)
    monkeypatch.setattr("TikTokLive.TikTokLiveClient", lambda **_: client)

    async def collect():
        comments = TikTokAdapter().stream_comments("alpha", "102", asyncio.Event(), on_connect=lambda: None)
        async with asyncio.timeout(2):
            assert (await anext(comments)).message == "切断前のコメント"
            with pytest.raises(RuntimeError, match="receiver failed"):
                await anext(comments)

    asyncio.run(collect())
    assert client.disconnected and client.web_closed


def test_tiktok_comment_stream_cancellation_reaps_receiver(monkeypatch):
    async def run():
        receiving = asyncio.Event()

        async def receive(_handler):
            receiving.set()
            await asyncio.Future()

        client = MockTikTokClient(receive)
        monkeypatch.setattr("TikTokLive.TikTokLiveClient", lambda **_: client)
        comments = TikTokAdapter().stream_comments("alpha", "102", asyncio.Event(), on_connect=lambda: None)
        read = asyncio.create_task(anext(comments))
        async with asyncio.timeout(2):
            await receiving.wait()
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        assert client.receiver.cancelled()
        assert client.disconnected and client.web_closed

    asyncio.run(run())


def test_offline_during_comment_connection_is_classified_separately(monkeypatch):
    from TikTokLive.client.errors import UserOfflineError

    async def start(**kwargs):
        raise UserOfflineError()

    client = MockTikTokClient(None)
    client.start = start
    monkeypatch.setattr("TikTokLive.TikTokLiveClient", lambda **_: client)

    async def collect():
        comments = TikTokAdapter().stream_comments("alpha", "102", asyncio.Event(), on_connect=lambda: None)
        with pytest.raises(CommentConnectionError) as result:
            await anext(comments)
        assert result.value.code == "comment_connection_not_ready"

    asyncio.run(collect())
    assert client.disconnected and client.web_closed


def test_duplicate_comment_id_is_kept_once(db):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=False,
        comments=True,
    )
    now = utcnow()
    apply_snapshot(
        db,
        streamer,
        Snapshot("LIVE", platform_stream_id="room-1"),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    from sqlalchemy import select

    from live_rec_lab.db.models import StreamSession

    session = db.scalars(select(StreamSession)).one()
    item = CommentIn(message="こんにちは", platform_comment_id="9", nickname="a", source="test")
    assert store_comment(db, session.id, item) is True
    assert store_comment(db, session.id, item) is False
    same_text = CommentIn(message="こんにちは", nickname="a", source="test")
    assert store_comment(db, session.id, same_text) is True


def test_collection_label_distinguishes_missing_and_zero():
    assert collection_label(None) == "未収集"
    job = CommentCollectionJob(
        stream_session_id=1,
        status="COLLECTING",
        received_count=0,
        stored_count=0,
        error_count=0,
        reconnect_count=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    assert collection_label(job) == "0"


def test_recording_offset_before_start_is_empty(db):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=True,
        comments=True,
    )
    now = utcnow()
    apply_snapshot(
        db,
        streamer,
        Snapshot("LIVE", platform_stream_id="room-1", started_at=now),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    from sqlalchemy import select

    from live_rec_lab.db.models import RecordingJob, StreamSession

    session = db.scalars(select(StreamSession)).one()
    db.add(
        RecordingJob(
            stream_session_id=session.id,
            status="RECORDING",
            retry_count=0,
            manual_stop_requested=False,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    store_comment(
        db,
        session.id,
        CommentIn(
            message="早い",
            platform_comment_id="1",
            posted_at=now - timedelta(seconds=5),
            source="test",
        ),
    )
    comment = db.scalars(select(Comment)).one()
    assert comment.recording_offset_ms is None
    assert comment.stream_offset_ms is not None and comment.stream_offset_ms < 0


def test_store_keeps_original_long_text_and_marks_srt_input(db):
    from live_rec_lab.db.models import RecordingJob, RecordingSegment, StreamSession

    now = utcnow()
    streamer = register_streamer(
        db, platform="tiktok", account="long", display_name="", monitoring=True, recording=True, comments=True,
    )
    stream = StreamSession(streamer_id=streamer.id, platform_stream_id="100", status="LIVE", detected_at=now)
    db.add(stream)
    db.flush()
    job = RecordingJob(stream_session_id=stream.id, status="COMPLETED", started_at=now)
    db.add(job)
    db.flush()
    segment = RecordingSegment(
        recording_job_id=job.id, stream_session_id=stream.id, started_at=now, duration_ms=10_000,
        validation_status="valid", srt_input_version=0, srt_generated_from_version=0,
    )
    db.add(segment)
    db.flush()

    message = "配信🎉\n" * 1000
    item = CommentIn(message=message, posted_at=now, platform_comment_id="x", source="test")
    assert store_comment(db, stream.id, item)
    assert db.query(Comment).one().message == message
    assert segment.srt_input_version == 1
    assert not store_comment(db, stream.id, item)
    assert segment.srt_input_version == 1
