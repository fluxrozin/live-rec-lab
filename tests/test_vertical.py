"""録画・コメント縦疎通（ローカル疑似配信 + 実ffmpeg + 一時SQLite）。外部通信なし。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from conftest import FakeAdapter
from live_rec_lab import recording, streamers
from live_rec_lab.db.models import comment_collection_jobs, comments, recording_segments, stream_sessions
from live_rec_lab.supervisor import Supervisor, SupervisorPolicy


def test_record_comments_srt_roundtrip(ctx):
    asyncio.run(_scenario(ctx))


async def _scenario(ctx):
    with ctx.engine.begin() as conn:
        st = streamers.register(conn, streamers.StreamerInput("tiktok", "@Sample_One", "架空配信者", True, True, True))
    adapter = FakeAdapter(seconds=30)
    sup = Supervisor(ctx, {"tiktok": adapter}, SupervisorPolicy(
        ending_grace_s=0.5, comment_retry_wait_s=0.2,
        recording=recording.Policy(poll_s=0.2, start_timeout_s=20, stall_timeout_s=10, backoff_base_s=0.2)))
    # 検知 → セッション → 録画/コメント独立起動
    await sup.handle_status(st, await adapter.check("sample_one"))
    assert len(sup.runs) == 1
    run = next(iter(sup.runs.values()))
    await asyncio.sleep(3.5)  # 録画を継続したまま配信終了（子プロセスはINTで回収され、取得済みが確定する）
    adapter.ended = True
    await sup.handle_status(st, await adapter.check("sample_one"))  # → ENDING
    assert sup.runs[run.session_id].ending_since is not None
    await asyncio.sleep(0.6)
    await sup.handle_status(st, await adapter.check("sample_one"))  # 猶予後の再確認 → 収束
    assert run.session_id not in sup.runs

    with ctx.engine.begin() as conn:
        sess = dict(conn.execute(select(stream_sessions)).mappings().one())
        segs = [dict(r) for r in conn.execute(select(recording_segments)).mappings()]
        n_comments = conn.execute(select(func.count()).select_from(comments)).scalar()
        job = dict(conn.execute(select(comment_collection_jobs)).mappings().one())
    assert sess["status"] == "ENDED" and sess["end_reason"] == "confirmed"
    valid = [s for s in segs if s["validation_status"] == "valid"]
    assert len(valid) == 1 and valid[0]["segment_number"] == 1
    video = ctx.env.recording_root / valid[0]["file_path"]
    assert video.exists() and video.suffix == ".mp4" and video.stat().st_size == valid[0]["file_size"]
    # 命名: platform_streamer_YYYYMMDD_HHMMSS_title_s001
    assert video.name.startswith("tiktok_sample_one_") and video.stem.endswith("_s001")
    assert "<" not in video.name and ":" not in video.name
    assert video.parent.parent.name == "sample_one"
    # 一時ファイルが残っていない
    assert not list(video.parent.glob("*.lrl-tmp"))
    # コメント: 8件受信、ID重複2件を排除、6件保存（c0..c5）
    assert job["received_count"] == 8 and job["duplicate_count"] == 2 and n_comments == 6
    assert job["status"] == "COMPLETED"
    # SRT
    srt_path = video.with_name(video.stem + "_comment.srt")
    assert valid[0]["srt_status"] == "generated" and srt_path.exists()
    text = srt_path.read_text("utf-8")
    assert "00:00:0" in text and "(ユーザー" in text and "😀" in text
    # 非上書き: 同名の確定は拒否される
    from live_rec_lab import storage

    tmp = storage.new_tmp_path(video)
    tmp.write_bytes(b"x")
    with pytest.raises(storage.AlreadyExists):
        storage.commit_new(tmp, video)
    assert tmp.exists()


def test_app_shutdown_finalizes_and_recovers(ctx):
    """アプリ停止: 録画の子プロセスを回収し取得済みを確定、セッションは終了未確認で収束（確認済み終了と区別）。"""

    async def go():
        with ctx.engine.begin() as conn:
            st = streamers.register(conn, streamers.StreamerInput("tiktok", "stop_one", None, True, True, True))
        adapter = FakeAdapter(seconds=60)
        sup = Supervisor(ctx, {"tiktok": adapter}, SupervisorPolicy(
            comment_retry_wait_s=0.2, recording=recording.Policy(poll_s=0.2, start_timeout_s=20, stall_timeout_s=10)))
        await sup.handle_status(st, await adapter.check("stop_one"))
        await asyncio.sleep(3.0)
        await sup.stop()

    asyncio.run(go())
    with ctx.engine.begin() as conn:
        sess = dict(conn.execute(select(stream_sessions)).mappings().one())
        segs = [dict(r) for r in conn.execute(select(recording_segments)).mappings()]
    assert sess["status"] == "ENDED" and sess["end_reason"] == "unconfirmed" and sess["platform_ended_at"] is None
    assert [s["validation_status"] for s in segs] == ["valid"]
    from live_rec_lab.supervisor import Supervisor as S

    # 再起動時回収: 未確定セグメントとLIVE残骸の整理（一時ファイルは保全）
    with ctx.engine.begin() as conn:
        conn.execute(recording_segments.update().values(validation_status="pending"))
        conn.execute(stream_sessions.update().values(status="LIVE", end_reason=None))
    S(ctx, {}, SupervisorPolicy())._recover()
    with ctx.engine.begin() as conn:
        assert conn.execute(select(recording_segments.c.validation_status)).scalar() == "invalid"
        assert conn.execute(select(stream_sessions.c.end_reason)).scalar() == "unconfirmed"


def test_retry_budget_is_finite_and_persistent(ctx):
    """空試行（即時失敗）は有限回で打ち切られ、予算は再開/再検知で復活せず、空試行は採番しない。"""
    from live_rec_lab import settings_store
    from live_rec_lab.db.models import retry_budgets
    from live_rec_lab.platforms.base import RecordSpec

    class FailingAdapter(FakeAdapter):
        async def record_spec(self, account, status, out_path, *, quality, engine_order):
            self.record_calls += 1
            return RecordSpec(["/bin/false"], "fail", out_format="ts")

    async def go():
        with ctx.engine.begin() as conn:
            settings_store.update(conn, {"reconnect_max_attempts": 2}, None)
            st = streamers.register(conn, streamers.StreamerInput("tiktok", "fail_one", None, True, True, False))
        adapter = FailingAdapter()
        sup = Supervisor(ctx, {"tiktok": adapter}, SupervisorPolicy(
            recording=recording.Policy(poll_s=0.1, start_timeout_s=5, stall_timeout_s=5, backoff_base_s=0.1, backoff_cap_s=0.2)))
        await sup.handle_status(st, await adapter.check("fail_one"))
        run = next(iter(sup.runs.values()))
        await asyncio.wait_for(run.rec_task, 30)
        assert adapter.record_calls == 3  # 初回 + 追加2回
        await sup.handle_status(st, await adapter.check("fail_one"))  # 再検知しても新予算は作られない
        await asyncio.sleep(0.5)
        assert adapter.record_calls == 3
        await sup.stop()

    asyncio.run(go())
    with ctx.engine.begin() as conn:
        b = dict(conn.execute(select(retry_budgets)).mappings().one())
        assert b["used_extra"] == 2 and b["suppression"] == "exhausted"
        assert conn.execute(select(func.count()).select_from(recording_segments)).scalar() == 0  # 空試行は行を残さない


def test_late_comments_are_reflected_after_reconvergence(ctx):
    """再開後に再収束したセッションは、遅れて届いたコメントを反映するようSRT/ASSを作り直す（起動時の後処理）。"""
    import datetime as dt

    from sqlalchemy import update

    from live_rec_lab import sessions
    from live_rec_lab.db.models import comments as ctable

    async def go():
        with ctx.engine.begin() as conn:
            st = streamers.register(conn, streamers.StreamerInput("tiktok", "late_one", None, True, True, True))
        ad = FakeAdapter(seconds=30)
        sup = Supervisor(ctx, {"tiktok": ad}, SupervisorPolicy(
            ending_grace_s=0.5, comment_retry_wait_s=0.2,
            recording=recording.Policy(poll_s=0.2, start_timeout_s=20, stall_timeout_s=10, backoff_base_s=0.2)))
        await sup.handle_status(st, await ad.check("late_one"))
        await asyncio.sleep(3.0)
        ad.ended = True
        await sup.handle_status(st, await ad.check("late_one"))
        await asyncio.sleep(0.6)
        await sup.handle_status(st, await ad.check("late_one"))
        return sup

    sup = asyncio.run(go())
    with ctx.engine.begin() as conn:
        seg = dict(conn.execute(select(recording_segments)).mappings().one())
        sid = seg["stream_session_id"]
        # 最初の字幕生成より後にコメントが追加され、セッションが再収束した状況を再現
        t = sessions.fmt_ts(sessions.parse_ts(seg["started_at"]) + dt.timedelta(seconds=1))
        conn.execute(ctable.insert().values(stream_session_id=sid, platform_comment_id="late-1", comment_source="ws",
                                             message="遅れて届いた", received_at=t, effective_at=t, created_at=t))
        conn.execute(update(stream_sessions).where(stream_sessions.c.id == sid).values(
            collection_converged_at=sessions.fmt_ts(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5))))
    asyncio.run(sup._postprocess_pending())
    with ctx.engine.begin() as conn:
        srt_path = conn.execute(select(recording_segments.c.srt_path)).scalar()
    assert srt_path and "遅れて届いた" in (ctx.env.recording_root / srt_path).read_text("utf-8")


def test_manual_stop_then_explicit_resume(ctx):
    """手動停止は自動再開されず、明示再開（新しい予算世代）でだけ新しい録画を始める。"""
    from live_rec_lab.db.models import retry_budgets

    async def go():
        with ctx.engine.begin() as conn:
            st = streamers.register(conn, streamers.StreamerInput("tiktok", "resume_one", None, True, True, False))
        ad = FakeAdapter(seconds=30)
        sup = Supervisor(ctx, {"tiktok": ad}, SupervisorPolicy(
            recording=recording.Policy(poll_s=0.2, start_timeout_s=20, stall_timeout_s=10, backoff_base_s=0.2)))
        await sup.handle_status(st, await ad.check("resume_one"))
        run = next(iter(sup.runs.values()))
        await asyncio.sleep(2.5)
        assert await sup.stop_recording(run.session_id)
        await asyncio.wait_for(run.rec_task, 30)
        await sup.handle_status(st, await ad.check("resume_one"))  # 同じ配信の監視では自動再開しない
        await asyncio.sleep(0.5)
        calls_before = ad.record_calls
        assert run.rec_task.done()
        assert await sup.resume(run.session_id, "recording") == "resumed"
        await asyncio.sleep(2.0)
        assert ad.record_calls == calls_before + 1 and not run.rec_task.done()
        await sup.stop()

    asyncio.run(go())
    with ctx.engine.begin() as conn:
        gens = [r[0] for r in conn.execute(select(retry_budgets.c.generation).where(retry_budgets.c.kind == "recording"))]
    assert sorted(gens) == [1, 2]
