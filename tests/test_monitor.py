"""監視の一時失敗、回復、対象ごとの再確認を隔離DBで検証する。"""

import asyncio
import json

from sqlalchemy import select

from live_rec_lab import streamers
from live_rec_lab.db.models import stream_sessions, system_events
from live_rec_lab.platforms.base import LIVE, OFFLINE, UNKNOWN, LiveStatus
from live_rec_lab.supervisor import Supervisor, SupervisorPolicy


def test_monitor_warns_once_and_records_recovery(ctx):
    with ctx.engine.begin() as conn:
        streamer = streamers.register(conn, streamers.StreamerInput("tiktok", "sample", None, True, False, False))

    def events():
        with ctx.engine.begin() as conn:
            return conn.execute(select(system_events).where(system_events.c.event_type.like("monitor.%"))
                                .order_by(system_events.c.id)).mappings().all()

    async def scenario():
        sup = Supervisor(ctx, {"tiktok": object()}, SupervisorPolicy(ending_grace_s=0))
        timeout = LiveStatus(UNKNOWN, reason="timeout_read")
        offline = LiveStatus(OFFLINE, explicit_end=True)
        await sup.handle_status(streamer, timeout)
        assert sup.diag[streamer["id"]]["state"] == UNKNOWN
        assert events() == []
        await sup.handle_status(streamer, offline)
        assert events() == []

        await sup.handle_status(streamer, LiveStatus(LIVE, stream_id="test-room"))
        await sup.handle_status(streamer, offline)  # ENDING
        await sup.handle_status(streamer, timeout)
        await sup.handle_status(streamer, timeout)
        await sup.handle_status(streamer, LiveStatus(UNKNOWN, reason="timeout_connect"))
        with ctx.engine.begin() as conn:
            assert conn.execute(select(stream_sessions.c.status)).scalar_one() == "ENDING"
        assert [r["event_type"] for r in events()] == ["monitor.unhealthy"]

        await sup.handle_status(streamer, offline)
        assert [r["event_type"] for r in events()] == ["monitor.unhealthy", "monitor.recovered"]
        recovered = json.loads(events()[-1]["metadata"])
        assert recovered["failure_count"] == 3
        assert recovered["reason"] == "timeout_connect"
        assert recovered["first_failure_at"] <= recovered["last_failure_at"] <= recovered["recovered_at"]
        with ctx.engine.begin() as conn:
            assert conn.execute(select(stream_sessions.c.end_reason)).scalar_one() == "confirmed"

    asyncio.run(scenario())


def test_monitor_does_not_overlap_checks_for_one_streamer(ctx, monkeypatch):
    with ctx.engine.begin() as conn:
        for account in ("slow", "fast"):
            streamers.register(conn, streamers.StreamerInput("tiktok", account, None, True, False, False))

    async def scenario():
        release = asyncio.Event()
        checked_fast_twice = asyncio.Event()
        calls = []

        class Adapter:
            external = False

            async def check(self, account):
                calls.append(account)
                if account == "slow":
                    await release.wait()
                elif calls.count("fast") == 2:
                    checked_fast_twice.set()
                return LiveStatus(OFFLINE, explicit_end=True)

        sup = Supervisor(ctx, {"tiktok": Adapter()}, SupervisorPolicy(tick_s=0.005))
        monkeypatch.setattr(sup, "settings", lambda: {"tiktok_monitor_rate": 6000})
        sup.tasks.append(asyncio.create_task(sup._monitor_loop("tiktok")))
        try:
            await asyncio.wait_for(checked_fast_twice.wait(), 2)
            assert calls[:3] == ["slow", "fast", "fast"]
        finally:
            release.set()
            await sup.stop()

    asyncio.run(scenario())


def test_monitor_backs_off_only_for_inactive_streamers(ctx):
    with ctx.engine.begin() as conn:
        idle, active = [streamers.register(conn, streamers.StreamerInput("tiktok", name, None, True, False, False))
                        for name in ("idle", "active")]

    async def scenario():
        sup = Supervisor(ctx, {"tiktok": object()})
        await sup.handle_status(active, LiveStatus(LIVE, stream_id="test-room"))
        for target in (idle, active):
            for _ in range(2):
                await sup.handle_status(target, LiveStatus(UNKNOWN, reason="timeout_check"))
        now = asyncio.get_running_loop().time()
        assert sup._next_monitor_target([idle, active], now) == active
        assert sup._next_monitor_target([idle], now) is None
        assert sup._next_monitor_target([idle], now + 16) == idle

        await sup.handle_status(idle, LiveStatus(OFFLINE, explicit_end=True))
        assert sup._next_monitor_target([idle], now) == idle

    asyncio.run(scenario())
