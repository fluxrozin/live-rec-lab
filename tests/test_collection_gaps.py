"""終了確認の前後と実際の復旧を隔離DBで検証する。"""

import asyncio
import sys

import pytest
from sqlalchemy import select

from conftest import FakeAdapter
from live_rec_lab import recording, settings_store, streamers
from live_rec_lab.db.models import collection_gaps
from live_rec_lab.platforms.base import RecordSpec
from live_rec_lab.supervisor import Supervisor, SupervisorPolicy


def gaps(ctx):
    with ctx.engine.connect() as conn:
        return conn.execute(select(collection_gaps)).mappings().all()


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("scenario", ["ended", "recovered", "still_live"])
def test_recording_gaps_span_empty_attempts(ctx, scenario):
    class Adapter(FakeAdapter):
        async def record_spec(self, account, status, out_path, **kwargs):
            if self.record_calls == 0 or (scenario == "recovered" and self.record_calls == 3):
                return await super().record_spec(account, status, out_path, **kwargs)
            self.record_calls += 1
            return RecordSpec([sys.executable, "-c", "pass"], "empty")

    async def run():
        with ctx.engine.begin() as conn:
            settings_store.update(conn, {"speech_subtitles_enabled": False}, expected_version=None)
            streamer = streamers.register(conn, streamers.StreamerInput("twitcasting", "fixture", None, True, True, False))
        adapter = Adapter(seconds=1)
        supervisor = Supervisor(ctx, {"twitcasting": adapter}, SupervisorPolicy(
            recording=recording.Policy(poll_s=0.05, backoff_base_s=0.03), ending_grace_s=0))
        try:
            await asyncio.wait_for(supervisor.handle_status(streamer, await adapter.check("fixture")), 8)
            active = next(iter(supervisor.runs.values()))
            await wait_until(lambda: adapter.record_calls >= 3)
            if scenario == "recovered":
                await wait_until(lambda: active.recorder.kept_segments == 2)
            elif scenario == "still_live":
                await supervisor.handle_status(streamer, await adapter.check("fixture"))
            adapter.ended = True
            for _ in range(2):
                await asyncio.wait_for(supervisor.handle_status(streamer, await adapter.check("fixture")), 8)
        finally:
            await supervisor.stop()

    asyncio.run(run())
    rows = gaps(ctx)
    expected = {"ended": [], "recovered": ["recording_reconnect"], "still_live": ["interrupted"]}
    assert [row["reason"] for row in rows] == expected[scenario]
    assert all(row["job_type"] == "recording" for row in rows)
    if scenario == "recovered":
        assert rows[0]["start_basis"] == rows[0]["end_basis"] == "approximate"
    elif scenario == "still_live":
        assert rows[0]["ended_at"] is None and rows[0]["end_basis"] == "unknown"


@pytest.mark.parametrize("scenario", ["ended", "recovered", "still_live"])
def test_comment_gaps_require_verified_recovery(ctx, scenario):
    ready = asyncio.Event()

    class Connection:
        on_ready = None

        def __init__(self, attempt):
            self.attempt = attempt

        def __aiter__(self):
            return self.receive()

        async def receive(self):
            if self.attempt == 1 or (scenario == "recovered" and self.attempt == 4):
                if self.on_ready:
                    await self.on_ready()
                if self.attempt == 4:
                    ready.set()
                    await asyncio.Future()
            else:
                raise ConnectionError("fixture")
            if False:
                yield

        async def aclose(self):
            pass

    class Adapter(FakeAdapter):
        attempts = 0

        def comments(self, account, status):
            self.attempts += 1
            return Connection(self.attempts)

    async def run():
        with ctx.engine.begin() as conn:
            streamer = streamers.register(conn, streamers.StreamerInput("tiktok", "fixture", None, True, False, True))
        adapter = Adapter()
        supervisor = Supervisor(ctx, {"tiktok": adapter}, SupervisorPolicy(
            comment_retry_wait_s=0.02, ending_grace_s=0))
        try:
            await asyncio.wait_for(supervisor.handle_status(streamer, await adapter.check("fixture")), 8)
            await wait_until(lambda: adapter.attempts >= 4)
            if scenario == "recovered":
                await asyncio.wait_for(ready.wait(), 5)
            elif scenario == "still_live":
                await supervisor.handle_status(streamer, await adapter.check("fixture"))
            adapter.ended = True
            for _ in range(2):
                await asyncio.wait_for(supervisor.handle_status(streamer, await adapter.check("fixture")), 8)
        finally:
            await supervisor.stop()

    asyncio.run(run())
    rows = gaps(ctx)
    expected = {"ended": [], "recovered": ["reconnected"], "still_live": ["interrupted"]}
    assert [row["reason"] for row in rows] == expected[scenario]
    assert all(row["job_type"] == "comments" for row in rows)
    if scenario == "still_live":
        assert rows[0]["ended_at"] is None and rows[0]["end_basis"] == "unknown"
