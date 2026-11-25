"""Windows媒体（/mnt/d の専用試験フォルダ）の代表確認。--run-hardware が必要（許可済み: D:\\LiveRecorder）。"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from live_rec_lab import recording, storage, streamers
from live_rec_lab.config import Environment
from live_rec_lab.db.models import recording_segments
from live_rec_lab.supervisor import Supervisor, SupervisorPolicy

pytestmark = pytest.mark.hardware
WIN_ROOT = Path("/mnt/d/LiveRecorder")


@pytest.fixture
def win(tmp_path):
    if not WIN_ROOT.is_dir():
        pytest.skip("D:\\LiveRecorder がありません")
    d = WIN_ROOT / f"pytest-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_non_overwrite_commit_and_case_insensitive_conflict(win):
    final = win / "tiktok_a_20261109_210000_夜の雑談_s001.mp4"
    tmp1 = storage.new_tmp_path(final)
    tmp1.write_bytes(b"first")
    ident = storage.commit_new(tmp1, final)
    assert final.read_bytes() == b"first" and not tmp1.exists() and ident["size"] == 5
    # 同名（大小文字違いを含む）は上書きされず、一時ファイルが保全される
    for name in (final.name, final.name.upper()):
        tmp2 = storage.new_tmp_path(final)
        tmp2.write_bytes(b"second")
        with pytest.raises(storage.AlreadyExists):
            storage.commit_new(tmp2, final.with_name(name))
        assert tmp2.exists() and final.read_bytes() == b"first"
        tmp2.unlink()
    # 管理済み字幕の置換は世代一致のときだけ
    srt = win / "x_comment.srt"
    g1 = storage.create_text_new(srt, "1\n")
    g2 = storage.replace_managed(srt, "2\n", g1)
    assert srt.read_text() == "2\n" and g2["size"] == 2
    srt.write_text("外部変更!!")  # 外部置換
    with pytest.raises(storage.StorageError):
        storage.replace_managed(srt, "3\n", g2)
    assert srt.read_text() == "外部変更!!"


def test_vertical_on_windows_mount(ctx, win):
    from conftest import FakeAdapter

    ctx.env = Environment(True, ctx.env.data_dir, ctx.env.state_dir, win, 8083)
    from live_rec_lab import settings_store

    with ctx.engine.begin() as conn:
        settings_store.update(conn, {"recording_root": str(win)}, None)
        st = streamers.register(conn, streamers.StreamerInput("tiktok", "win_sample", None, True, True, True))
    adapter = FakeAdapter(seconds=30)
    sup = Supervisor(ctx, {"tiktok": adapter}, SupervisorPolicy(
        ending_grace_s=0.5, comment_retry_wait_s=0.2,
        recording=recording.Policy(poll_s=0.2, start_timeout_s=20, stall_timeout_s=10, backoff_base_s=0.2)))

    async def scenario():
        await sup.handle_status(st, await adapter.check("win_sample"))
        await asyncio.sleep(3.5)
        adapter.ended = True
        await sup.handle_status(st, await adapter.check("win_sample"))
        await asyncio.sleep(0.6)
        await sup.handle_status(st, await adapter.check("win_sample"))

    asyncio.run(scenario())
    with ctx.engine.begin() as conn:
        segs = [dict(r) for r in conn.execute(select(recording_segments)).mappings()]
    valid = [s for s in segs if s["validation_status"] == "valid"]
    assert len(valid) == 1
    video = win / valid[0]["file_path"]
    assert video.exists() and video.stat().st_size == valid[0]["file_size"]
    assert video.with_name(video.stem + "_comment.srt").exists()
    assert not list(win.rglob("*.lrl-tmp"))
