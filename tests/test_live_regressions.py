"""実配信試験で見つかった不具合の最小回帰（2026-11-25）。外部通信なし。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

from live_rec_lab import media_tools
from live_rec_lab.platforms.base import LiveStatus
from live_rec_lab.platforms.twitcasting import TwitCastingAdapter, TwitCastingComments


def test_comments_never_use_slice_id_zero(monkeypatch):
    """0件の間に slice_id=0 を送ると公式APIが400（実測）。1以上のIDを得るまで offset 取得を続ける。"""
    calls = []

    class A(TwitCastingAdapter):
        async def _get(self, path, params=None):
            calls.append(dict(params or {}))
            if len(calls) < 3:
                return 200, {"comments": []}
            return 200, {"comments": [{"id": 7, "message": "hi", "created": 1790000000, "from_user": {"screen_id": "u", "name": "n"}}]}

    from live_rec_lab.external import ExternalGate

    a = A(ExternalGate(), None, {}, lambda: 5.0)
    a.anonymous_ws = False  # このテストは公式APIのページ取得だけを見る（補助WebSocketは別テスト）
    st = LiveStatus("LIVE", stream_id="1")
    stream = TwitCastingComments(a, st)

    async def go():
        async def no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)  # 待機を省略（テスト終了時に元へ戻る）
        got = []
        async for ev in stream:
            got.append(ev)
            break
        return got

    got = asyncio.run(go())
    assert got and got[0].platform_comment_id == "7"
    assert all("slice_id" not in c for c in calls[:3])  # 0件の間はoffsetのみ


def test_live_flv_without_duration_is_probed_but_remuxable(tmp_path):
    """ライブFLVは読取時に長さが無い（実測）。映像コーデックは識別でき、MP4化後は長さが取れる。"""
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    flv, mp4 = tmp_path / "live.flv", tmp_path / "out.mp4"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", "-f", "flv", "-flvflags",
                    "no_duration_filesize", str(flv)], check=True)
    pr = media_tools.probe(fp, flv)
    assert pr.video_codec == "h264"  # 判定条件: コーデック識別可なら remux へ進む
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(flv), "-c", "copy", "-f", "mp4", str(mp4)], check=True)
    assert media_tools.probe(fp, mp4).ok


def test_https_support_detected():
    t = media_tools.resolve("/usr/bin")
    assert t.https is True  # Ubuntu標準ffmpegはTLS対応。TLS非対応ビルドではFalseになりStreamlinkへ切替


def test_access_restriction_stops_both_jobs(ctx):
    """制限が確実に判明したら録画・コメントの双方を停止し、制限は回避せず、同じ配信を再開しない（要求§4.2.5）。"""
    from sqlalchemy import select

    from conftest import FakeAdapter
    from live_rec_lab import recording, streamers
    from live_rec_lab.db.models import retry_budgets, stream_sessions
    from live_rec_lab.platforms.base import AccessRestricted
    from live_rec_lab.supervisor import Supervisor, SupervisorPolicy

    class Restricted(FakeAdapter):
        def comments(self, account, status):
            class S:
                def __aiter__(self):
                    return self._g()

                async def _g(self):
                    raise AccessRestricted("age_restricted")
                    yield

                async def aclose(self):
                    return None

            return S()

    async def go():
        with ctx.engine.begin() as conn:
            st = streamers.register(conn, streamers.StreamerInput("tiktok", "restr_one", None, True, True, True))
        ad = Restricted(seconds=60)
        sup = Supervisor(ctx, {"tiktok": ad}, SupervisorPolicy(
            recording=recording.Policy(poll_s=0.2, start_timeout_s=20, stall_timeout_s=10)))
        await sup.handle_status(st, await ad.check("restr_one"))
        for _ in range(160):
            await asyncio.sleep(0.25)
            if not sup.runs:
                break
        assert not sup.runs
        await sup.handle_status(st, await ad.check("restr_one"))  # 同じ配信の再検知でも再開しない
        assert not sup.runs
        await sup.stop()

    asyncio.run(go())
    with ctx.engine.begin() as conn:
        s = dict(conn.execute(select(stream_sessions)).mappings().one())
        sup_reasons = {r[0] for r in conn.execute(select(retry_budgets.c.suppression))}
    assert s["status"] == "ENDED" and s["end_reason"] == "restricted" and sup_reasons == {"restricted"}


def test_twitcasting_preroll_classification_and_trim(tmp_path):
    """実配信で観測した公式HLSのプリロール（黒地に…、60フレーム周期×2）: 内容照合で除外/トリムし、本編は保全する。"""
    from pathlib import Path

    from live_rec_lab.platforms import twitcasting_preroll as pre

    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    fx = Path(__file__).parent / "fixtures" / "twitcasting_preroll.mp4"
    assert pre.classify_head(ff, fx, 4070).is_preroll
    # 本編のみ（別解像度の合成映像）はプリロールではない
    main = tmp_path / "main.ts"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=704x396:rate=30:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-f", "mpegts", str(main)], check=True)
    assert not pre.classify_head(ff, main, 6000).is_preroll
    assert pre.find_main_start(ff, fp, main) is None
    # プリロール + 本編が1つの録画に続く場合、本編の開始位置を特定できる（先頭のプリロール約4秒）
    pre_ts = tmp_path / "pre.ts"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", str(fx), "-c", "copy", "-f", "mpegts", str(pre_ts)], check=True)
    both = tmp_path / "both.ts"
    lst = tmp_path / "list.txt"
    lst.write_text(f"file '{pre_ts}'\nfile '{main}'\n")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    "-f", "mpegts", str(both)], check=True)
    start = pre.find_main_start(ff, fp, both)
    assert start is not None and 3.5 < start < 4.6


def test_anonymous_number_from_pubsub_matches_by_comment_id():
    """実測した視聴用WebSocketの形式。番号は名前末尾の #数字、コメントIDは公式APIと共通。匿名以外・番号なしは対象外。"""
    from live_rec_lab.platforms.twitcasting import parse_pubsub_message

    raw = ('[{"type":"comment","id":34022851296,"message":"456","createdAt":1795586254000,"author":{"id":"c:tw3",'
           '"name":"\\u533f\\u540d\\u30b3\\u30e1\\u30f3\\u30c8#579","screenName":"c:tw3","grade":0},"isAnonymous":true,"numComments":3}]')
    assert parse_pubsub_message(raw) == [("34022851296", "579")]
    assert parse_pubsub_message("[]") == [] and parse_pubsub_message("not json") == []
    normal = '[{"type":"comment","id":1,"author":{"name":"user#12"},"isAnonymous":false}]'
    assert parse_pubsub_message(normal) == []
    nonum = '[{"type":"comment","id":2,"author":{"name":"匿名コメント"},"isAnonymous":true}]'
    assert parse_pubsub_message(nonum) == []


def test_anonymous_label_format():
    """利用者指定: 匿名は「匿名コメント#番号」（番号不明は「匿名コメント」）。通常IDによる同定は避ける。"""
    from live_rec_lab import srt

    assert srt.author_label("匿名コメント", "c:tw3", True, "579") == " (匿名コメント#579)"
    assert srt.author_label("匿名コメント", None, True, None) == " (匿名コメント)"
    assert srt.author_label("太郎", "@taro", False, None) == " (太郎 | @taro)"


def test_public_tc_hls_selection():
    """公開tc-hls: streamserver.php の tc-hls.streams から品質順に選び、許可ホストのHTTPSだけを返す（Streamlinkプラグインと同形式）。"""
    import httpx

    from live_rec_lab.external import ExternalGate
    from live_rec_lab.platforms.twitcasting import TwitCastingAdapter

    payload = {"movie": {"id": 1, "live": True}, "tc-hls": {"streams": {
        "low": "https://202-1-1-1.twitcasting.tv/l.m3u8", "medium": "https://202-1-1-1.twitcasting.tv/m.m3u8",
        "high": "https://evil.example.com/h.m3u8"}}}
    class StubLock:  # 製品の共通ロック（共有状態）には触れない
        held = True

    lock = StubLock()
    try:
        gate = ExternalGate(True, lock)

        class A(TwitCastingAdapter):
            pass

        a = A(gate, None, {})

        orig = gate.client

        def fake_client(service, **kw):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))

        gate.client = fake_client  # type: ignore[method-assign]
        # best/high: high(許可外ホスト)は使わず medium へ
        assert asyncio.run(a._public_hls_url("u", "best")) == "https://202-1-1-1.twitcasting.tv/m.m3u8"
        assert asyncio.run(a._public_hls_url("u", "medium")) == "https://202-1-1-1.twitcasting.tv/m.m3u8"
        payload["movie"]["live"] = False
        assert asyncio.run(a._public_hls_url("u", "best")) is None
        gate.client = orig  # type: ignore[method-assign]
    finally:
        pass


def test_tiktok_room_api_stream_id_is_not_room_id():
    """room APIの streamId は『ストリームID』で接続先Room IDではない（実測）。配信IDとして保持し、WebSocketのRoom IDはSDKが解決する。"""
    import json

    from live_rec_lab.platforms.tiktok import parse_room_response

    body = json.dumps({"data": {"liveRoom": {"status": 2, "streamId": "1561000000000000001", "title": "t"}}}).encode()
    st = parse_room_response(200, "application/json", body)
    assert st.state == "LIVE" and st.stream_id == "1561000000000000001"
