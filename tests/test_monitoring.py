import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from live_rec_lab.config import utcnow, validate_setting
from live_rec_lab.db.models import SystemEvent
from live_rec_lab.monitoring.scheduler import initial_last_check, personal_period
from live_rec_lab.platforms.base import Snapshot
from live_rec_lab.platforms.tiktok import TikTokAdapter, check_failure_pauses, default_probe, room_snapshot
from live_rec_lab.platforms.twitcasting import TwitCastingAdapter
from live_rec_lab.services.jobs import Coordinator
from live_rec_lab.services.sessions import apply_snapshot, register_streamer
from live_rec_lab.storage import normalize_account, recording_paths


def test_normalize_tiktok_url_and_reject_traversal():
    parsed = normalize_account("tiktok", "https://www.tiktok.com/@name.ok/live")
    assert parsed.platform_user_id == "name.ok"
    with pytest.raises(ValueError):
        normalize_account("tiktok", "https://evil.example/@name")
    with pytest.raises(ValueError):
        normalize_account("twitcasting", "https://twitcasting.tv/caster")


def test_duplicate_streamer(db):
    register_streamer(
        db,
        platform="tiktok",
        account="@alpha",
        display_name="",
        monitoring=True,
        recording=True,
        comments=False,
    )
    with pytest.raises(ValueError):
        register_streamer(
            db,
            platform="tiktok",
            account="alpha",
            display_name="A",
            monitoring=True,
            recording=True,
            comments=True,
        )


def test_unknown_is_not_offline(db):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=True,
        comments=True,
    )
    apply_snapshot(
        db,
        streamer,
        Snapshot("UNKNOWN", detail="timeout"),
        utcnow(),
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    assert streamer.live_status == "UNKNOWN"


def test_unknown_past_grace_keeps_the_live_session(db):
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
        Snapshot("LIVE", platform_stream_id="room-1", title="live"),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    from live_rec_lab.db.models import StreamSession

    session = db.scalars(select(StreamSession)).one()
    seen = session.last_seen_live_at
    intents = apply_snapshot(
        db,
        streamer,
        Snapshot("UNKNOWN", detail="配信状態を確認できません"),
        now + timedelta(seconds=40),
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    assert session.status == "LIVE"
    assert session.last_seen_live_at == seen
    assert all(item.kind not in {"stop_recording", "stop_comments"} for item in intents)


def test_room_status_separates_offline_from_http_failure():
    body = {"data": {"user": {"roomId": "99"}, "liveRoom": {"status": 2, "roomId": "99"}}}
    assert room_snapshot(200, body).status == "LIVE"
    prepare = room_snapshot(200, {"data": {"liveRoom": {"status": 1, "roomId": "7711"}}})
    paused = room_snapshot(200, {"data": {"liveRoom": {"status": 3, "roomId": "7711"}}})
    assert prepare.status == "UNKNOWN"
    assert prepare.detail == "room_status_1"
    assert paused.status == "UNKNOWN"
    assert paused.detail == "room_status_3"
    assert room_snapshot(200, {"data": {"liveRoom": {"status": 1}}}).status == "UNKNOWN"
    assert room_snapshot(200, {"data": {"liveRoom": {"status": 5, "roomId": "1"}}}).detail == "room_status_5"
    assert room_snapshot(200, {"data": {"liveRoom": {"status": 4}}}).status == "OFFLINE"
    denied = room_snapshot(403, "")
    empty = room_snapshot(200, None)
    assert denied.status == "ERROR"
    assert denied.detail == "http_403"
    assert empty.detail == "invalid_json"
    assert check_failure_pauses(denied.detail)
    assert not check_failure_pauses(empty.detail)
    assert check_failure_pauses("http_429")
    assert check_failure_pauses("http_503")
    assert not check_failure_pauses("http_404")


def test_unconfirmed_room_status_does_not_start_or_end(db):
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
    idle = apply_snapshot(
        db,
        streamer,
        room_snapshot(200, {"data": {"liveRoom": {"status": 1, "roomId": "7711"}}}),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    assert all(item.kind != "start_recording" for item in idle)
    from live_rec_lab.db.models import StreamSession

    assert db.scalars(select(StreamSession)).first() is None
    apply_snapshot(
        db,
        streamer,
        Snapshot("LIVE", platform_stream_id="7711"),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    session = db.scalars(select(StreamSession)).one()
    seen = session.last_seen_live_at
    later = apply_snapshot(
        db,
        streamer,
        room_snapshot(200, {"data": {"liveRoom": {"status": 3, "roomId": "7711"}}}),
        now + timedelta(seconds=40),
        grace=30,
        recorder_alive=lambda _session_id: True,
    )
    assert session.status == "LIVE"
    assert session.last_seen_live_at == seen
    assert all(item.kind not in {"stop_recording", "stop_comments"} for item in later)


@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        (httpx.ConnectError, "connect_error"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.ReadError, "network_error"),
        (httpx.RemoteProtocolError, "protocol_error"),
        (httpx.ProxyError, "proxy_error"),
        (httpx.DecodingError, "invalid_encoding"),
        (httpx.TooManyRedirects, "request_error"),
    ],
)
def test_tiktok_probe_reports_transport_failures(monkeypatch, error_type, detail):
    async def send(_client, _request):
        raise error_type("internal diagnostic")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    snap = asyncio.run(default_probe("name"))
    assert snap.status == "UNKNOWN"
    assert snap.detail == detail


def test_monitor_events_identify_the_platform_for_matching_accounts(db):
    streamer_ids = {}
    for platform in ("tiktok", "twitcasting"):
        streamer = register_streamer(
            db,
            platform=platform,
            account="same",
            display_name="",
            monitoring=True,
            recording=False,
            comments=False,
        )
        streamer_ids[platform] = streamer.id
    db.commit()

    async def probe(_account):
        return Snapshot("UNKNOWN", detail="network_error")

    def send(_request):
        raise httpx.ReadError("internal diagnostic")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            coordinator = Coordinator(TikTokAdapter(probe), TwitCastingAdapter("id", "secret", client=client))
            for platform, streamer_id in streamer_ids.items():
                await coordinator.check_streamer(streamer_id, platform, "same", 30, {})
                await coordinator.check_streamer(streamer_id, platform, "same", 30, {})

    asyncio.run(run())
    messages = db.scalars(
        select(SystemEvent.message).where(SystemEvent.event_type == "monitor_detail").order_by(SystemEvent.id)
    ).all()
    assert messages == ["tiktok / same: network_error", "twitcasting / same: network_error"]


@pytest.mark.parametrize("body", [b"", b"<html>invalid</html>"], ids=["empty", "html"])
def test_invalid_json_does_not_pause_the_next_check(monkeypatch, body):
    calls = []

    async def send(_client, request):
        calls.append(request.url.params["uniqueId"])
        return httpx.Response(200, content=body)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    adapter = TikTokAdapter()

    async def run() -> tuple[Snapshot, Snapshot]:
        return await adapter.check("name"), await adapter.check("name")

    first, second = asyncio.run(run())
    assert first == second == Snapshot("ERROR", detail="invalid_json")
    assert calls == ["name", "name"]


@pytest.mark.parametrize("status_code", [403, 429, 503])
@pytest.mark.parametrize("body", [b"", b"<html>unavailable</html>", b"{}"], ids=["empty", "html", "json"])
def test_tiktok_http_failure_pauses_only_the_affected_account(monkeypatch, status_code, body):
    calls = []

    async def send(_client, request):
        account = request.url.params["uniqueId"]
        calls.append(account)
        if account == "name":
            return httpx.Response(status_code, content=body)
        return httpx.Response(200, json={"data": {"liveRoom": {"status": 2, "roomId": "1"}}})

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    adapter = TikTokAdapter()

    async def run() -> tuple[Snapshot, Snapshot, Snapshot]:
        return await adapter.check("name"), await adapter.check("name"), await adapter.check("other")

    first, second, other = asyncio.run(run())
    assert first == Snapshot("ERROR", detail=f"http_{status_code}")
    assert second.detail == "確認を一時停止しています"
    assert other == Snapshot("LIVE", platform_stream_id="1")
    assert calls == ["name", "other"]


def test_short_offline_does_not_end_session(db):
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
    intents = apply_snapshot(
        db,
        streamer,
        Snapshot("LIVE", platform_stream_id="room-1", title="live"),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    assert any(item.kind == "start_recording" for item in intents)
    later = now + timedelta(seconds=10)
    apply_snapshot(
        db,
        streamer,
        Snapshot("OFFLINE"),
        later,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    from live_rec_lab.db.models import StreamSession

    session = db.scalars(select(StreamSession)).one()
    assert session.status == "ENDING"
    apply_snapshot(
        db,
        streamer,
        Snapshot("OFFLINE"),
        now + timedelta(seconds=40),
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    assert session.status == "ENDED"


def test_manual_stop_blocks_auto_recording(db):
    streamer = register_streamer(
        db,
        platform="tiktok",
        account="alpha",
        display_name="A",
        monitoring=True,
        recording=True,
        comments=False,
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
    from live_rec_lab.db.models import StreamSession

    session = db.scalars(select(StreamSession)).one()
    session.manual_stop = True
    intents = apply_snapshot(
        db,
        streamer,
        Snapshot("LIVE", platform_stream_id="room-1"),
        now,
        grace=30,
        recorder_alive=lambda _session_id: False,
    )
    assert all(item.kind != "start_recording" for item in intents)


def test_paths_do_not_overwrite_across_services(tmp_path):
    started = utcnow()
    _, _, tiktok = recording_paths(tmp_path, "tiktok", "same", started, "Asia/Tokyo", 1)
    _, _, twicas = recording_paths(tmp_path, "twitcasting", "same", started, "Asia/Tokyo", 1)
    assert tiktok.parent == twicas.parent
    assert tiktok.name.startswith("tiktok_")
    assert twicas.name.startswith("twitcasting_")


def test_each_streamer_keeps_a_separate_check_time():
    period = personal_period(6, 2)
    assert period == 12
    now = 1_000.0
    first = initial_last_check(now, period, 0, 2)
    second = initial_last_check(now, period, 1, 2)
    assert now - first >= period
    assert now - second < period
    assert personal_period(6, 1) == 6


def test_monitor_rate_bounds():
    assert validate_setting("monitor_per_minute_tiktok", 10) == 10
    with pytest.raises(ValueError):
        validate_setting("monitor_per_minute_tiktok", 11)
    with pytest.raises(ValueError):
        validate_setting("monitor_per_minute_twitcasting", 0)
    assert validate_setting("monitor_per_minute_twitcasting", 60) == 60


def test_grace_floor():
    from live_rec_lab.monitoring.scheduler import grace_seconds, service_interval

    assert service_interval(10) == 6
    assert grace_seconds(10) == 30
    assert grace_seconds(1) == 120


def test_first_explicit_offline_starts_grace_even_after_stale_live(db):
    from live_rec_lab.db.models import StreamSession

    streamer = register_streamer(
        db, platform="tiktok", account="alpha", display_name="",
        monitoring=True, recording=True, comments=True,
    )
    now = utcnow()
    apply_snapshot(
        db, streamer, Snapshot("LIVE", platform_stream_id="123"), now,
        grace=15, recorder_alive=lambda _: True,
    )
    intents = apply_snapshot(
        db, streamer, Snapshot("OFFLINE"), now + timedelta(seconds=60),
        grace=15, recorder_alive=lambda _: True,
    )
    assert db.scalars(select(StreamSession)).one().status == "ENDING"
    assert not intents
    intents = apply_snapshot(
        db, streamer, Snapshot("OFFLINE"), now + timedelta(seconds=75),
        grace=15, recorder_alive=lambda _: True,
    )
    assert db.scalars(select(StreamSession)).one().status == "ENDED"
    assert {item.kind for item in intents} == {"stop_recording", "stop_comments"}


def test_room_false_or_zero_id_never_starts_job():
    for room in (False, 0, "000"):
        snap = room_snapshot(200, {"data": {"liveRoom": {"status": 2, "roomId": room}}})
        assert snap.status == "UNKNOWN"


def test_tiktok_does_not_truncate_comment_text():
    from types import SimpleNamespace

    from live_rec_lab.platforms.tiktok import comment_from_event

    raw = "長文🙂\n" * 700
    item = comment_from_event(SimpleNamespace(content=raw, user=None, common=None))
    assert item is not None
    assert item.message == raw


def test_orphan_deadline_starts_only_after_both_jobs_stop_and_reuses_session(db):
    from live_rec_lab.db.models import CollectionGap, StreamSession

    streamer = register_streamer(
        db, platform="tiktok", account="alpha", display_name="",
        monitoring=True, recording=True, comments=True,
    )
    now = utcnow()
    alive = [True, False]

    def apply(snapshot, seconds):
        return apply_snapshot(
            db, streamer, snapshot, now + timedelta(seconds=seconds),
            grace=15, recorder_alive=lambda _: alive[0], comments_alive=lambda _: alive[1], orphan_recheck=120,
        )

    started = apply(Snapshot("LIVE", platform_stream_id="123"), 0)
    session = db.scalars(select(StreamSession)).one()
    assert all(intent.session_id == session.id for intent in started)
    apply(Snapshot("UNKNOWN"), 200)
    assert session.status == "LIVE"
    alive[0], alive[1] = False, True
    apply(Snapshot("ERROR"), 400)
    assert session.status == "LIVE"
    alive[1] = False
    apply(Snapshot("UNKNOWN"), 600)
    apply(Snapshot("UNKNOWN"), 719)
    assert session.status == "LIVE"
    apply(Snapshot("ERROR"), 720)
    assert session.status == "ENDED_UNCONFIRMED"
    assert session.ended_at == now + timedelta(seconds=720)
    assert session.platform_ended_at is None
    assert session.end_reason == "platform_end_unconfirmed"
    assert {gap.job_type for gap in db.scalars(select(CollectionGap))} == {"recording", "comments"}
    apply(Snapshot("LIVE", platform_stream_id="123"), 721)
    assert session.status == "LIVE"
    assert session.ended_at is None


def test_tiktok_sdk_delayed_events_are_drained_when_stopping(monkeypatch):
    from types import SimpleNamespace

    import TikTokLive

    class FakeClient:
        def __init__(self, *, unique_id):
            self.handlers = {}
            self.release = asyncio.Event()
            self.web = SimpleNamespace(close=self.close_http)
            self.http_closed = False
            self.receiver = None

        def on(self, event):
            def register(handler):
                self.handlers[event.__name__] = handler
                return handler
            return register

        async def start(self, *, room_id, fetch_live_check):
            assert room_id == 902
            assert fetch_live_check is False

            async def receive():
                self.handlers["ConnectEvent"](SimpleNamespace(room_id=902))
                await self.release.wait()
                self.handlers["CommentEvent"](SimpleNamespace(content="最後のコメント🙂"))

            self.receiver = asyncio.create_task(receive())
            return self.receiver

        async def disconnect(self):
            self.release.set()
            if self.receiver is not None:
                await self.receiver

        async def close_http(self):
            self.http_closed = True

    clients = []

    def make_client(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(TikTokLive, "TikTokLiveClient", make_client)

    async def run():
        stop = asyncio.Event()
        connected = asyncio.Event()

        async def consume():
            adapter = TikTokAdapter()
            return [
                item.message async for item in adapter.stream_comments(
                    "caster", "902", stop, on_connect=connected.set,
                )
            ]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(connected.wait(), timeout=1)
        assert not task.done()
        stop.set()
        assert await asyncio.wait_for(task, timeout=1) == ["最後のコメント🙂"]

    asyncio.run(run())
    assert clients[0].http_closed
    assert clients[0].receiver.done()
