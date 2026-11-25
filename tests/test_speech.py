"""音声SRT・統合ASS: 合成した日本語音声（Windows SAPI）を実GPUで文字起こしする。--run-hardware とモデルが必要。"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from live_rec_lab import ass, speech, streamers
from live_rec_lab.db.models import comments, recording_segments, stream_sessions
from live_rec_lab.runtime import utcnow
from live_rec_lab.sessions import fmt_ts
from live_rec_lab.supervisor import Supervisor, SupervisorPolicy

FIXTURE = Path(__file__).parent / "fixtures" / "speech_ja.mp4"
DEV_MODELS = Path.home() / ".local/share/live-rec-lab-dev/data/models"


def test_ass_escape_and_lanes():
    assert ass.escape("a{\\b1}b\n\n😀c") == "a｛＼b1｝b\\Nc"
    text, diag = ass.build(640, 360, [(0, 2000, "こんにちは")], [(0, 5000, f"c{i}") for i in range(20)], 10000)
    assert diag["omitted_comments"] > 0 and "Style: Speech" in text and "Style: Comment" in text
    assert speech.clamp_cues([{"start": -1, "end": 2, "text": "x"}, {"start": 1, "end": 99, "text": "ok"},
                              {"start": 20, "end": 30, "text": "out"}, {"start": 3, "end": 3, "text": " "}], 10000) == [(1000, 10000, "ok")]
    assert speech.parse_srt("1\n00:00:01,000 --> 00:00:02,500\nあ\nい\n\n") == [(1000, 2500, "あ\nい")]


@pytest.mark.hardware
def test_gpu_transcription_and_ass(ctx):
    if not (DEV_MODELS / "models--Systran--faster-whisper-large-v3").exists():
        pytest.skip("モデル未取得（devtools fetch-model large-v3）")
    ctx.env.model_dir.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(DEV_MODELS, ctx.env.model_dir)  # 承認済みの読取専用共有（書込・取得はしない）
    root = ctx.env.recording_root
    video_rel = "tt/2026-11-25/tiktok_tt_20261125_120000_speech_s001.mp4"
    (root / video_rel).parent.mkdir(parents=True)
    shutil.copy(FIXTURE, root / video_rel)
    start = datetime.now(UTC) - timedelta(minutes=5)
    with ctx.engine.begin() as conn:
        st = streamers.register(conn, streamers.StreamerInput("tiktok", "tt", None, True, True, True))
        sid = conn.execute(stream_sessions.insert().values(
            streamer_id=st["id"], detected_at=fmt_ts(start), status="ENDED", end_reason="confirmed", recording_root=str(root),
            created_at=utcnow(), updated_at=utcnow())).inserted_primary_key[0]
        from live_rec_lab.db.models import recording_jobs

        job = conn.execute(recording_jobs.insert().values(stream_session_id=sid, status="COMPLETED", created_at=utcnow(),
                                                          updated_at=utcnow())).inserted_primary_key[0]
        seg = conn.execute(recording_segments.insert().values(
            recording_job_id=job, stream_session_id=sid, segment_number=1, started_at=fmt_ts(start), file_path=video_rel,
            duration_ms=15000, validation_status="valid", container_format="mp4", created_at=utcnow(),
            file_identity="{}")).inserted_primary_key[0]
        for i, sec in enumerate((2, 6)):
            t = fmt_ts(start + timedelta(seconds=sec))
            conn.execute(insert(comments).values(stream_session_id=sid, platform_comment_id=f"c{i}", nickname=f"U{i}",
                                                  comment_source="ws", message=f"コメント{i}", received_at=t, effective_at=t, created_at=t))
    sup = Supervisor(ctx, {}, SupervisorPolicy())
    res = asyncio.run(sup.speech.process_segment(seg))
    assert res["status"] == "generated" and res["cues"] >= 1, res
    with ctx.engine.begin() as conn:
        row = dict(conn.execute(select(recording_segments)).mappings().one())
    assert row["speech_status"] == "generated" and row["ass_status"] == "generated"
    srt_text = (root / row["speech_srt_path"]).read_text("utf-8")
    assert "ライブ" in srt_text or "配信" in srt_text
    ass_text = (root / row["ass_path"]).read_text("utf-8")
    assert "Dialogue: 0," in ass_text and "Dialogue: 1," in ass_text and "PlayResX: 320" in ass_text
