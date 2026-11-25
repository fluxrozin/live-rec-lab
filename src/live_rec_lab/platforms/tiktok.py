"""TikTokアダプター: 公開room APIの監視、Streamlink/yt-dlp録画、TikTokLive(+Euler署名)コメント。

* 監視は署名を消費しない公開room APIを第一候補とする（要求§4.1.2）。HTTP状態を本文解析より先に判定。
* `liveRoom.status`: 2=配信中（Room IDあり）, 4=終了, 1/3/その他=UNKNOWN（終了にも配信中にもしない）。
* 実応答の構造・Room IDの項目名は実配信で確認する（採用版: TikTokLive 7.0.1 / Streamlink 8.6.3）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from datetime import UTC, datetime

import httpx

from live_rec_lab.external import Budget, ExternalDenied, ExternalGate, host_allowed
from live_rec_lab.platforms.base import (
    ERROR,
    LIVE,
    OFFLINE,
    UNKNOWN,
    AccessRestricted,
    CommentEvent,
    LiveStatus,
    RecordSpec,
)

log = logging.getLogger("live_rec_lab.tiktok")
ROOM_API = "https://www.tiktok.com/api-live/user/room"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# 署名（Euler Stream）接続の計上。無料キーの実測（/accounts/me/rate_limits, 2026-11-25）: 分60・時500・日1500、
# 接続1回で残枠が1減る。枠の数値は固定せず、無料枠の1/16程度に抑えた保守的な上限（30回/時間）で計上する。
SIGN_LIMIT, SIGN_WINDOW_S = 30, 3600.0
ROOM_LIMIT_PER_MIN = 12  # 監視の絶対上限（設定上限10回/分より少し余裕）


def parse_room_response(http_status: int, content_type: str, body: bytes, retry_after: str | None = None) -> LiveStatus:
    """HTTPステータス先行で判定し、room応答をLiveStatusへ変換する（純粋関数）。"""
    if http_status == 429:
        st = LiveStatus(UNKNOWN, reason="http_429")
        st.extra["retry_after"] = retry_after
        return st
    if http_status == 403:
        return LiveStatus(UNKNOWN, reason="http_403")
    if http_status >= 500:
        return LiveStatus(UNKNOWN, reason="http_5xx")
    if http_status != 200:
        return LiveStatus(UNKNOWN, reason=f"http_{http_status}")
    if not body or not body.strip():
        return LiveStatus(UNKNOWN, reason="empty_body")
    if "json" not in (content_type or "").lower() and body.lstrip()[:1] in (b"<",):
        return LiveStatus(UNKNOWN, reason="html_body")
    try:
        data = json.loads(body)
    except ValueError:
        return LiveStatus(UNKNOWN, reason="invalid_json")
    if not isinstance(data, dict):
        return LiveStatus(UNKNOWN, reason="unexpected_shape")
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    room = inner.get("liveRoom") if isinstance(inner, dict) else None
    if not isinstance(room, dict):
        return LiveStatus(UNKNOWN, reason="no_live_room")
    status = room.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return LiveStatus(UNKNOWN, reason="no_status")
    if status == 4:
        return LiveStatus(OFFLINE, explicit_end=True, reason="status_4")
    if status == 2:
        # 注意(実測 2026-11-25): room API の `streamId` はストリームIDで、WebSocketの接続先Room IDとは別の値。
        # 接続時にSDKが解決するRoom ID（ConnectEvent.room_id / room_info.id）と、room_info.stream_id で照合する。
        rid = room.get("streamId") or room.get("roomId") or room.get("id")
        if rid in (None, "", 0, "0"):
            return LiveStatus(UNKNOWN, reason="no_room_id")
        title = room.get("title") if isinstance(room.get("title"), str) else None
        started = None
        for key in ("startTime", "createTime"):
            v = room.get(key)
            if isinstance(v, int) and v > 1_500_000_000:
                started = datetime.fromtimestamp(v / 1000 if v > 10**11 else v, UTC)
                break
        return LiveStatus(LIVE, stream_id=str(rid), title=title, started_at=started)
    return LiveStatus(UNKNOWN, reason=f"status_{status}")


class TikTokAdapter:
    name = "tiktok"
    external = True

    def __init__(self, gate: ExternalGate, budget: Budget, secrets: dict[str, str]) -> None:
        self.gate, self.budget, self.secrets = gate, budget, secrets
        self._client: httpx.AsyncClient | None = None
        self._engine_idx: dict[str, int] = {}
        self.engine_stats: dict[str, dict] = {}

    # ------------------------------------------------------------ monitor
    async def check(self, account: str) -> LiveStatus:
        if not await asyncio.to_thread(self.budget.reserve, "tiktok:room", ROOM_LIMIT_PER_MIN, 60.0):
            return LiveStatus(UNKNOWN, reason="budget_wait")
        wait = await asyncio.to_thread(self.budget.waiting_until, "tiktok:room")
        if wait:
            return LiveStatus(UNKNOWN, reason="rate_limited_wait")
        if self._client is None:
            self._client = self.gate.client("tiktok", headers={"User-Agent": UA, "Accept": "application/json"})
        params = {"aid": 1988, "sourceType": 54, "staleTime": 600000, "uniqueId": account}
        try:
            r = await self._client.get(ROOM_API, params=params, headers={"Referer": f"https://www.tiktok.com/@{account}/live"})
        except ExternalDenied:
            raise
        except httpx.TimeoutException:
            return LiveStatus(UNKNOWN, reason="timeout")
        except httpx.ConnectError as exc:
            return LiveStatus(UNKNOWN, reason="dns_error" if "Name or service" in str(exc) else "connect_error")
        except httpx.HTTPError as exc:
            return LiveStatus(ERROR, reason=f"http_error:{type(exc).__name__}")
        st = parse_room_response(r.status_code, r.headers.get("content-type", ""), r.content, r.headers.get("retry-after"))
        if st.reason == "http_429":
            try:
                ra = float(st.extra.get("retry_after") or 60)
            except ValueError:
                ra = 60.0
            await asyncio.to_thread(self.budget.wait_until, "tiktok:room", time.time() + max(ra, 30.0))
        return st

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------ recording
    def _quality_names(self, quality: str) -> str:
        return {"best": "best", "high": "h264_hd,h264_sd,best", "medium": "h264_sd,h264_ld,worst"}.get(quality, "best")

    async def record_spec(self, account: str, status: LiveStatus, out_path: str, *, quality: str,
                          engine_order: list[str]) -> RecordSpec | None:
        self.gate.check("tiktok", f"https://www.tiktok.com/@{account}/live")
        order = [e for e in engine_order if e in ("streamlink", "yt-dlp")]
        if not order:
            return None
        engine = order[self._engine_idx.get(account, 0) % len(order)]
        url = f"https://www.tiktok.com/@{account}/live"
        if engine == "streamlink":
            return RecordSpec([sys.executable, "-m", "streamlink", "--loglevel", "error", "--force", "-o", out_path,
                               url, self._quality_names(quality)], "streamlink", out_format="flv")
        return RecordSpec([sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part", "--no-mtime",
                           "--no-playlist", "-o", out_path, url], "yt-dlp", out_format="flv")

    def record_result(self, account: str, engine: str, kept: bool, reason: str | None) -> None:
        """実際にデータを取得できたエンジンを維持。失敗が続くときだけ次の候補へ切り替える。"""
        s = self.engine_stats.setdefault(engine, {"last_success": None, "last_failure": None, "reason": None, "failures": 0})
        now = datetime.now(UTC).isoformat()
        if kept:
            s.update(last_success=now, failures=0)
            return
        s.update(last_failure=now, reason=reason, failures=s["failures"] + 1)
        if s["failures"] >= 1:
            self._engine_idx[account] = self._engine_idx.get(account, 0) + 1
            log.info("録画エンジンを切り替えます: %s → 次の候補（%s）", engine, reason)

    # ------------------------------------------------------------ comments
    def comments(self, account: str, status: LiveStatus) -> TikTokComments:
        return TikTokComments(self, account, status)


class TikTokComments:
    """TikTokLive CommentEvent → CommentEvent。Room ID一致を確認し、受信タスク寿命を管理する。"""

    def __init__(self, adapter: TikTokAdapter, account: str, status: LiveStatus) -> None:
        self.adapter, self.account, self.status = adapter, account, status
        self._client = None
        self.discarded_mismatch = 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        from TikTokLive import TikTokLiveClient
        from TikTokLive.client.errors import AgeRestrictedError, UserOfflineError
        from TikTokLive.events import CommentEvent as TTComment
        from TikTokLive.events import ConnectEvent, DisconnectEvent, LiveEndEvent

        a = self.adapter
        key = a.secrets.get("SIGN_API_KEY")
        if not key:
            raise RuntimeError("SIGN_API_KEY が未設定です（署名を要するコメント接続を保留）")
        stream_id = str(self.status.stream_id or "")
        if not stream_id:
            raise RuntimeError("配信IDが確認できないためコメント接続を保留します")
        if not await asyncio.to_thread(a.budget.reserve, "eulerstream:connect", SIGN_LIMIT, SIGN_WINDOW_S):
            raise RuntimeError("署名枠の保守的な計上が上限のため接続を見送りました")
        wait = await asyncio.to_thread(a.budget.waiting_until, "eulerstream:connect")
        if wait:
            raise RuntimeError("署名枠の待機期限内のため接続を見送りました")

        async def hook(request: httpx.Request) -> None:
            h = request.url.host
            if not (host_allowed("tiktok", h) or host_allowed("eulerstream", h)):
                raise ExternalDenied(f"許可されていない宛先です: {h}")
            a.gate.check("tiktok" if host_allowed("tiktok", h) else "eulerstream", str(request.url))

        client = TikTokLiveClient(unique_id=self.account, web_kwargs={
            "httpx_kwargs": {"event_hooks": {"request": [hook]}}, "signer_kwargs": {"sign_api_key": key}})
        self._client = client
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        state: dict = {"ended": False, "connected": False, "room_id": None}

        @client.on(ConnectEvent)
        async def _on_connect(ev: ConnectEvent) -> None:
            info = client.room_info if isinstance(client.room_info, dict) else {}
            got = str(info.get("stream_id_str") or info.get("stream_id") or "")
            ok = bool(got) and got == stream_id
            state["room_id"] = ev.room_id
            log.info("TikTokLive 接続イベント: Room ID %s（監視対象の配信IDと%s）", ev.room_id, "一致" if ok else "不一致/確認不能")
            if not ok:  # 別配信・確認不能のイベントは保存せず、接続を破棄して再確認する
                await q.put(RuntimeError("接続先の配信IDが監視対象と一致しない/確認できません"))
            state["connected"] = True

        @client.on(LiveEndEvent)
        async def _on_end(_ev) -> None:
            log.info("TikTokLive: 配信終了イベント")
            state["ended"] = True
            await q.put(None)

        @client.on(DisconnectEvent)
        async def _on_disc(_ev) -> None:
            log.info("TikTokLive: 切断イベント")
            await q.put(None)

        @client.on(TTComment)
        async def _on_comment(ev: TTComment) -> None:
            common = getattr(ev, "common", None)
            rid = getattr(common, "room_id", 0) or 0
            if rid and state.get("room_id") and rid != state["room_id"]:
                self.discarded_mismatch += 1
                return
            await q.put(_to_comment(ev))

        try:
            # 実測(2026-11-25): Room IDを渡して start(room_id=..., fetch_live_check=False) とすると、SDKがHTMLから得るセッション情報
            # （Cookie等）が揃わず、接続できてもチャットが配信されない（RoomMessage 1件のみ）。参照実装(fluxrozin/live-recorder)と同じく
            # Room IDを渡さず fetch_room_info=True で接続するとチャットが届く。Room IDの一致は ConnectEvent で検証する。
            task = await client.start(fetch_room_info=True)
            log.info("TikTokLive: 受信タスクを開始しました（配信ID %s）", stream_id)
        except UserOfflineError:
            return
        except AgeRestrictedError as exc:
            # 正規の接続手順（HTML取得後）で room info が取得できない場合のみ制限とみなす（参照実装と同じ扱い）。回避はしない
            with contextlib.suppress(Exception):
                await client._web.close()
            raise AccessRestricted("age_restricted") from exc
        try:
            while True:
                get = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    item = get.result()
                    if item is None:
                        return
                    if isinstance(item, Exception):
                        raise item
                    yield item
                else:
                    get.cancel()
                    exc = task.exception() if not task.cancelled() else None
                    while not q.empty():  # 受信タスク終了後も残件を処理（遅延受信の取りこぼし防止）
                        item = q.get_nowait()
                        if isinstance(item, CommentEvent):
                            yield item
                    if exc is not None:
                        raise exc
                    return
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
            with contextlib.suppress(Exception):
                await client._web.close()

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()


def _to_comment(ev) -> CommentEvent:
    common = getattr(ev, "common", None)
    user = getattr(ev, "user", None)
    now = datetime.now(UTC)
    msg_id = getattr(common, "msg_id", 0) or 0
    created = getattr(common, "create_time", 0) or 0
    posted = None
    if created > 1_500_000_000:
        with contextlib.suppress(Exception):
            posted = datetime.fromtimestamp(created / 1000 if created > 10**11 else created, UTC)
    uid = (getattr(user, "display_id", "") or "") or (str(getattr(user, "id", 0)) if getattr(user, "id", 0) else None)
    nick = getattr(user, "nickname", None) or None
    return CommentEvent(message=getattr(ev, "content", "") or "", received_at=now,
                        platform_comment_id=str(msg_id) if msg_id else None, platform_user_id=uid or None,
                        nickname=nick, posted_at=posted, source="ws")
