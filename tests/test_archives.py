"""アーカイブ削除の安全性: 世代照合・保留・復活防止・稼働中拒否（実ファイル・一時DB）。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from live_rec_lab import archives, storage, streamers
from live_rec_lab.db.models import comments, deletion_operations, recording_jobs, recording_segments, stream_sessions
from live_rec_lab.runtime import utcnow


def _seed(ctx, status="ENDED", end_reason="confirmed", stream_id="s1"):
    root = ctx.env.recording_root
    with ctx.engine.begin() as conn:
        existing = streamers.list_active(conn)
        st = existing[0] if existing else streamers.register(
            conn, streamers.StreamerInput("tiktok", "del_one", None, True, True, True))
        sid = conn.execute(stream_sessions.insert().values(
            streamer_id=st["id"], platform_stream_id=stream_id, detected_at=utcnow(), status=status, end_reason=end_reason,
            ended_at=utcnow(), recording_root=str(root), created_at=utcnow(), updated_at=utcnow())).inserted_primary_key[0]
        job = conn.execute(recording_jobs.insert().values(stream_session_id=sid, status="COMPLETED", created_at=utcnow(),
                                                          updated_at=utcnow())).inserted_primary_key[0]
        d = root / "del_one" / "2026-11-25"
        d.mkdir(parents=True, exist_ok=True)
        v, c = d / f"v{sid}.mp4", d / f"v{sid}_comment.srt"
        v.write_bytes(b"video")
        c.write_text("1\n")
        ids = {**storage.identity(v), "srt": storage.identity(c)}
        conn.execute(recording_segments.insert().values(
            recording_job_id=job, stream_session_id=sid, segment_number=1, file_path=str(v.relative_to(root)), file_size=5,
            duration_ms=1000, validation_status="valid", srt_status="generated", srt_path=str(c.relative_to(root)),
            file_identity=json.dumps(ids), created_at=utcnow()))
        conn.execute(comments.insert().values(stream_session_id=sid, platform_comment_id="x", comment_source="ws", message="m",
                                              received_at=utcnow(), effective_at=utcnow(), created_at=utcnow()))
    return sid, v, c


def test_delete_completes_and_blocks_redetect(ctx):
    sid, v, c = _seed(ctx)
    res = archives.delete_archive(ctx.engine, ctx.env, sid)
    assert res["status"] == "completed" and not v.exists() and not c.exists()
    with ctx.engine.begin() as conn:
        assert conn.execute(select(func.count()).select_from(stream_sessions)).scalar() == 0
        assert conn.execute(select(func.count()).select_from(comments)).scalar() == 0
        assert conn.execute(select(deletion_operations.c.status)).scalar() == "完了"
    assert archives.was_deleted(ctx.env, "tiktok", "del_one", "s1")  # 復活防止の最小記録（本文なし）
    assert "message" not in archives.tombstone_file(ctx.env).read_text()


def test_replaced_file_is_held_and_db_kept(ctx):
    sid, v, c = _seed(ctx)
    v.write_bytes(b"EXTERNALLY REPLACED")  # 外部置換（別世代）
    res = archives.delete_archive(ctx.engine, ctx.env, sid)
    assert res["status"] == "partial" and v.exists() and v.read_bytes() == b"EXTERNALLY REPLACED"
    with ctx.engine.begin() as conn:  # 関連DBは残る
        assert conn.execute(select(func.count()).select_from(stream_sessions)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(comments)).scalar() == 1


def test_unmounted_root_is_not_treated_as_absent(ctx, tmp_path):
    sid, v, c = _seed(ctx)
    with ctx.engine.begin() as conn:
        conn.execute(stream_sessions.update().values(recording_root=str(tmp_path / "unplugged")))
    res = archives.delete_archive(ctx.engine, ctx.env, sid)
    assert res["status"] == "partial" and v.exists()


def test_refusals(ctx):
    sid, *_ = _seed(ctx, status="LIVE", end_reason=None, stream_id="live")
    with pytest.raises(archives.Refused):
        archives.delete_archive(ctx.engine, ctx.env, sid)
    sid2, *_ = _seed(ctx, end_reason="unconfirmed", stream_id="unc")
    with pytest.raises(archives.Refused):  # 終了未確認は手動収束まで不可
        archives.delete_archive(ctx.engine, ctx.env, sid2)
    with pytest.raises(archives.Refused):  # 自動削除は常に拒否
        archives.manual_converge(ctx.engine, sid2, "", False)
    archives.manual_converge(ctx.engine, sid2, "配信は終了していることを確認", False)
    with pytest.raises(archives.Refused):
        archives.delete_archive(ctx.engine, ctx.env, sid2, automatic=True)
    assert archives.delete_archive(ctx.engine, ctx.env, sid2)["status"] == "completed"
