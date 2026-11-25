from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from live_rec_lab import settings_store
from live_rec_lab.config import Environment
from live_rec_lab.context import AppContext
from live_rec_lab.db import session as dbs
from live_rec_lab.external import ExternalGate
from live_rec_lab.media_tools import resolve
from live_rec_lab.platforms.base import LIVE, OFFLINE, CommentEvent, LiveStatus, RecordSpec


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", help="実配信/外部APIの試験を許可（利用者許可が必要）")
    parser.addoption("--run-hardware", action="store_true", help="実GPU/Windows媒体の試験を許可")


def pytest_collection_modifyitems(config, items):
    for flag, mark in (("--run-live", "live"), ("--run-hardware", "hardware")):
        if config.getoption(flag):
            continue
        skip = pytest.mark.skip(reason=f"{flag} が必要です")
        for it in items:
            if mark in it.keywords:
                it.add_marker(skip)


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    base = tmp_path / "dev"
    env = Environment(True, base / "data", tmp_path / "state", base / "recordings", 8083)
    env.data_dir.mkdir(parents=True)
    env.recording_root.mkdir(parents=True)
    dbs.migrate(env.db_path)
    engine = dbs.make_engine(env.db_path)
    with engine.begin() as conn:
        settings_store.ensure_defaults(conn, {"recording_root": str(env.recording_root)})
    tools = resolve()
    return AppContext(env=env, engine=engine, gate=ExternalGate(False, None), tools=tools, instance_id="t",
                      started_at="2026-11-25T00:00:00.000Z", port=8083)


class FakeAdapter:
    """ローカル疑似配信: ffmpegが生成するTSを録画し、定期的にコメントを流す。外部通信はしない。"""

    name = "tiktok"
    external = False

    def __init__(self, seconds: int = 6) -> None:
        self.seconds = seconds
        self.ended = False
        self.record_calls = 0

    async def check(self, account: str) -> LiveStatus:
        if self.ended:
            return LiveStatus(OFFLINE, explicit_end=True)
        return LiveStatus(LIVE, stream_id="fake-room-1", title="夜の雑談: <テスト>", started_at=datetime.now(UTC))

    async def record_spec(self, account, status, out_path, *, quality, engine_order):
        self.record_calls += 1
        ff = shutil.which("ffmpeg")
        return RecordSpec([ff, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
                           "testsrc=size=320x240:rate=15", "-f", "lavfi", "-i", "sine=frequency=440", "-t",
                           str(self.seconds), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts",
                           out_path], "fake-ffmpeg", out_format="ts")

    def comments(self, account, status):
        return _FakeComments()


class _FakeComments:
    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for i in range(8):
            yield CommentEvent(message=f"こんばんは{i} 😀\n二行目", received_at=datetime.now(UTC),
                               platform_comment_id=f"c{i % 6}", platform_user_id=f"user{i}", nickname=f"ユーザー{i}")
            await asyncio.sleep(0.4)
        while True:
            await asyncio.sleep(3600)

    async def aclose(self):
        return None
