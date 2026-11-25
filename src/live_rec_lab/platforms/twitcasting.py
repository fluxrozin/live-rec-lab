"""ツイキャスアダプター: 公式API v2（Basic認証）の current_live / comments、公式HLS録画。

* 認証は Client ID/Secret の HTTP Basic のみ。資格情報が揃わなければ連携を保留する。
* 監視・コメント取得は共通の永続予算（60回/60秒の保守的な計上）とレスポンスの X-RateLimit-* を尊重する。
* プリロール除外・匿名番号WebSocketは実配信での実証後に追加する（番号は取得できなければNULL）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from live_rec_lab.external import Budget, ExternalDenied, ExternalGate, host_allowed
from live_rec_lab.platforms.base import ERROR, LIVE, OFFLINE, UNKNOWN, CommentEvent, LiveStatus, RecordSpec

log = logging.getLogger("live_rec_lab.twitcasting")
API = "https://apiv2.twitcasting.tv"
API_LIMIT, API_WINDOW = 55, 60.0  # 公式の60回/60秒に余裕を持たせた計上
PAGE = 50
MAX_BACKFILL_PAGES = 20


def parse_current_live(http_status: int, body: dict | None) -> LiveStatus:
    """current_live 応答 → LiveStatus（純粋関数）。404は『配信していない』として扱う。"""
    if http_status == 404:
        return LiveStatus(OFFLINE, explicit_end=True, reason="not_live_404")
    if http_status == 401:
        return LiveStatus(ERROR, reason="auth_error")
    if http_status == 403:
        code = ((body or {}).get("error") or {}).get("code")
        return LiveStatus(UNKNOWN, reason="api_limit" if code == 2000 else "http_403")
    if http_status == 429:
        return LiveStatus(UNKNOWN, reason="http_429")
    if http_status >= 500:
        return LiveStatus(UNKNOWN, reason="http_5xx")
    if http_status != 200 or not isinstance(body, dict):
        return LiveStatus(UNKNOWN, reason=f"http_{http_status}")
    movie = body.get("movie")
    if not isinstance(movie, dict) or not movie.get("id"):
        return LiveStatus(UNKNOWN, reason="no_movie")
    if movie.get("is_live") is not True:
        return LiveStatus(OFFLINE, explicit_end=True, reason="is_live_false")
    created = movie.get("created")
    started = datetime.fromtimestamp(created, UTC) if isinstance(created, int) and created > 1_000_000_000 else None
    st = LiveStatus(LIVE, stream_id=str(movie["id"]), title=movie.get("title") if isinstance(movie.get("title"), str) else None,
                    started_at=started)
    st.extra["hls_url"] = movie.get("hls_url") if isinstance(movie.get("hls_url"), str) else None
    st.extra["restricted"] = bool(movie.get("is_protected"))
    return st


def to_comment(c: dict) -> CommentEvent | None:
    cid = c.get("id")
    if cid is None or not isinstance(c.get("message"), str):
        return None
    user = c.get("from_user") if isinstance(c.get("from_user"), dict) else {}
    screen = user.get("screen_id") if isinstance(user.get("screen_id"), str) else None
    anonymous = bool(screen and screen.startswith("c:"))  # 共有匿名アカウント。番号は捏造しない
    created = c.get("created")
    posted = datetime.fromtimestamp(created, UTC) if isinstance(created, int) and created > 1_000_000_000 else None
    return CommentEvent(
        message=c["message"], received_at=datetime.now(UTC), platform_comment_id=str(cid),
        platform_user_id=None if anonymous else (screen or (str(user["id"]) if user.get("id") else None)),
        nickname=user.get("name") if isinstance(user.get("name"), str) else None,
        is_anonymous=True if anonymous else None, posted_at=posted, source="api")


class TwitCastingAdapter:
    name = "twitcasting"
    external = True

    def __init__(self, gate: ExternalGate, budget: Budget, secrets: dict[str, str],
                 poll_seconds: Callable[[], float] = lambda: 10.0, ffmpeg: str | None = None,
                 ffmpeg_https: bool = True) -> None:
        self.gate, self.budget, self.secrets, self.poll_seconds = gate, budget, secrets, poll_seconds
        self.ffmpeg, self.ffmpeg_https = ffmpeg, ffmpeg_https
        self._client: httpx.AsyncClient | None = None
        self._engine_idx: dict[str, int] = {}
        self.engine_stats: dict[str, dict] = {}

    @property
    def configured(self) -> bool:
        return "TWITCASTING_CLIENT_ID" in self.secrets and "TWITCASTING_CLIENT_SECRET" in self.secrets

    def _headers(self) -> dict[str, str]:
        raw = f"{self.secrets['TWITCASTING_CLIENT_ID']}:{self.secrets['TWITCASTING_CLIENT_SECRET']}".encode()
        return {"Accept": "application/json", "X-Api-Version": "2.0",
                "Authorization": "Basic " + base64.b64encode(raw).decode()}

    async def _get(self, path: str, params: dict | None = None) -> tuple[int, dict | None]:
        """予算計上 → GET → ヘッダの残枠を反映。JSONでなければ body=None。"""
        if not self.configured:
            raise ExternalDenied("ツイキャスの資格情報が未設定です（連携を保留）")
        if not await asyncio.to_thread(self.budget.reserve, "twitcasting:api", API_LIMIT, API_WINDOW):
            return 429, None
        if await asyncio.to_thread(self.budget.waiting_until, "twitcasting:api"):
            return 429, None
        if self._client is None:
            self._client = self.gate.client("twitcasting", base_url=API)
        r = await self._client.get(path, params=params, headers=self._headers())
        remaining, reset = r.headers.get("x-ratelimit-remaining"), r.headers.get("x-ratelimit-reset")
        try:
            if remaining is not None and int(remaining) <= 2 and reset:
                await asyncio.to_thread(self.budget.wait_until, "twitcasting:api", float(reset) + 1)
        except ValueError:
            pass
        if r.status_code in (403, 429) and reset:
            with_reset = float(reset) if reset.replace(".", "").isdigit() else time.time() + 60
            await asyncio.to_thread(self.budget.wait_until, "twitcasting:api", with_reset + 1)
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = None
        return r.status_code, body if isinstance(body, dict) else None

    # ------------------------------------------------------------ monitor
    async def check(self, account: str) -> LiveStatus:
        try:
            code, body = await self._get(f"/users/{account}/current_live")
        except ExternalDenied:
            raise
        except httpx.TimeoutException:
            return LiveStatus(UNKNOWN, reason="timeout")
        except httpx.ConnectError:
            return LiveStatus(UNKNOWN, reason="connect_error")
        except httpx.HTTPError as exc:
            return LiveStatus(ERROR, reason=f"http_error:{type(exc).__name__}")
        return parse_current_live(code, body)

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------ recording
    async def record_spec(self, account: str, status: LiveStatus, out_path: str, *, quality: str,
                          engine_order: list[str]) -> RecordSpec | None:
        if status.extra.get("restricted"):
            return None  # 合言葉等のアクセス制限は取得しない（回避しない）
        # 規定順: 公式API HLS → 公開tc-hls → Streamlink HLS（ffmpegがHTTPS非対応ならffmpeg経由の2つは使わない）
        engines = ["official-hls", "tc-hls", "streamlink"] if self.ffmpeg_https else ["streamlink"]
        if not self.ffmpeg_https:
            log.info("使用中のffmpegはHTTPS非対応のため、公式HLSの直接取得は使わずStreamlinkを使います")
        engine = engines[self._engine_idx.get(account, 0) % len(engines)]
        ff = self.ffmpeg or shutil.which("ffmpeg")
        url = status.extra.get("hls_url")
        if engine == "tc-hls":
            tc_url = await self._public_hls_url(account, quality)
            if tc_url is None:
                self._engine_idx[account] = self._engine_idx.get(account, 0) + 1
                engine = "streamlink"
            else:
                return RecordSpec(
                    [ff or "ffmpeg", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "hls,https,tls,tcp,crypto",
                     "-user_agent", "Mozilla/5.0", "-icy", "0", "-i", tc_url, "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                     "-flush_packets", "1", "-f", "mpegts", out_path], "tc-hls", out_format="ts")
        if engine == "official-hls" and url:
            host = urlsplit(url).hostname or ""
            if urlsplit(url).scheme != "https" or not host_allowed("twitcasting", host):
                log.warning("公式HLSのURLが許可されていないため使用しません: host=%s", host)
                self._engine_idx[account] = self._engine_idx.get(account, 0) + 1
                engine = "streamlink"
            else:
                self.gate.check("twitcasting", url)
                return RecordSpec(
                    [ff or "ffmpeg", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "hls,https,tls,tcp,crypto",
                     "-user_agent", "Mozilla/5.0", "-icy", "0", "-i", url, "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                     "-flush_packets", "1", "-f", "mpegts", out_path], "official-hls", out_format="ts")
        import sys

        page = f"https://twitcasting.tv/{account}"
        self.gate.check("twitcasting", page)
        return RecordSpec([sys.executable, "-m", "streamlink", "--loglevel", "error", "--force", "-o", out_path, page,
                           "best"], "streamlink", out_format="ts")

    async def _public_hls_url(self, account: str, quality: str) -> str | None:
        """公開の視聴用HLS（streamserver.php の tc-hls.streams）。形式は Streamlink 8.6.3 の twitcasting プラグインと同じ。

        品質: best/high は high→medium→low、medium は medium→low（要求§6.2）。許可ホストのHTTPSのみ。
        """
        order = ["medium", "low"] if quality == "medium" else ["high", "medium", "low"]
        try:
            self.gate.check("twitcasting", "https://twitcasting.tv/streamserver.php")
            client = self.gate.client("twitcasting")
            try:
                r = await client.get("https://twitcasting.tv/streamserver.php",
                                     params={"target": account, "mode": "client", "player": "pc_web"},
                                     headers={"User-Agent": "Mozilla/5.0"})
                data = r.json()
            finally:
                await client.aclose()
        except (ExternalDenied, httpx.HTTPError, ValueError) as exc:
            log.warning("公開tc-hlsの取得に失敗: %s", type(exc).__name__)
            return None
        movie, streams = data.get("movie") or {}, (data.get("tc-hls") or {}).get("streams") or {}
        if not movie.get("live") or not isinstance(streams, dict):
            return None
        for name in order:
            u = streams.get(name)
            if isinstance(u, str) and u.startswith("https://") and host_allowed("twitcasting", urlsplit(u).hostname or ""):
                self.gate.check("twitcasting", u)
                return u
        return None

    def inspect_head(self, tools, raw, probe) -> dict | None:
        """先頭のプリロール判定（公式HLS。内容照合で確認できた場合のみ除外/トリム。判別不能は本編として保全）。"""
        from live_rec_lab.platforms import twitcasting_preroll as pre

        v = pre.classify_head(tools.ffmpeg, raw, probe.duration_ms)
        if v.is_preroll:
            return {"action": "drop", "reason": v.reason}
        start = pre.find_main_start(tools.ffmpeg, tools.ffprobe, raw)
        if start is not None and start > 0:
            return {"action": "trim", "start": start, "reason": "先頭に既知のプリロールがあり、本編が続く"}
        return None

    def record_result(self, account: str, engine: str, kept: bool, reason: str | None) -> None:
        s = self.engine_stats.setdefault(engine, {"last_success": None, "last_failure": None, "reason": None, "failures": 0})
        now = datetime.now(UTC).isoformat()
        if kept:
            s.update(last_success=now, failures=0)
            return
        s.update(last_failure=now, reason=reason, failures=s["failures"] + 1)
        self._engine_idx[account] = self._engine_idx.get(account, 0) + 1

    # ------------------------------------------------------------ comments
    def comments(self, account: str, status: LiveStatus) -> TwitCastingComments:
        return TwitCastingComments(self, status)


PUBSUB_URL = "https://twitcasting.tv/eventpubsuburl.php"
_ANON_NUM = re.compile(r"#(\d{1,9})\s*$")


def parse_pubsub_message(raw: str) -> list[tuple[str, str]]:
    """視聴用WebSocketのメッセージから (コメントID, 匿名番号) を取り出す（純粋関数）。

    実測(2026-11-25): `[{"type":"comment","id":<API共通のコメントID>,"author":{"name":"匿名コメント#579",...},"isAnonymous":true}]`。
    番号は名前末尾の `#数字`。共通コメントIDで公式APIのコメントと一意に照合できる。曖昧なら返さない。
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out = []
    for ev in data if isinstance(data, list) else []:
        if not isinstance(ev, dict) or ev.get("type") != "comment" or ev.get("isAnonymous") is not True:
            continue
        author = ev.get("author") if isinstance(ev.get("author"), dict) else {}
        m = _ANON_NUM.search(str(author.get("name", "")))
        if m and ev.get("id") is not None:
            out.append((str(ev["id"]), m.group(1)))
    return out


class AnonymousNumbers:
    """匿名コメント番号の補助取得（非公式の視聴用WebSocket）。失敗しても公式APIの収集は止めない。"""

    def __init__(self, adapter: TwitCastingAdapter, movie_id: str) -> None:
        self.a, self.movie_id = adapter, movie_id
        self.numbers: dict[str, str] = {}
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._event = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"tc-anon-{self.movie_id}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def wait_for(self, comment_id: str, timeout: float) -> str | None:
        """APIで先に届いたコメントの番号を短時間待つ（WebSocketが少し遅れる場合）。"""
        end = asyncio.get_running_loop().time() + timeout
        while comment_id not in self.numbers:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                return None
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), left)
            except TimeoutError:
                return None
        return self.numbers[comment_id]

    async def _run(self) -> None:
        import websockets

        a = self.a
        while True:
            try:
                a.gate.check("twitcasting", PUBSUB_URL)
                client = a.gate.client("twitcasting")
                try:
                    r = await client.post(PUBSUB_URL, data={"movie_id": self.movie_id, "password": ""},
                                          headers={"User-Agent": "Mozilla/5.0"})
                    url = r.json()["url"]
                finally:
                    await client.aclose()
                a.gate.check("twitcasting", url.replace("wss://", "https://", 1))
                async with websockets.connect(url, origin="https://twitcasting.tv", open_timeout=15,
                                              additional_headers={"User-Agent": "Mozilla/5.0"}) as ws:
                    self.error = None
                    async for msg in ws:
                        for cid, num in parse_pubsub_message(msg if isinstance(msg, str) else msg.decode("utf-8", "replace")):
                            self.numbers[cid] = num
                            self._event.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 匿名番号の欠損として記録するだけ。コメント収集は止めない
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("匿名番号の補助取得が中断（番号は取得できない間NULL）: %s", self.error)
            await asyncio.sleep(10)


class TwitCastingComments:
    """slice_id による増分取得。50件で飽和したら offset で遡って境界まで確実に取得する。"""

    def __init__(self, adapter: TwitCastingAdapter, status: LiveStatus) -> None:
        self.a, self.movie_id = adapter, status.stream_id
        self.last_id = 0
        self.unreached = 0
        self._closed = False
        self.anon = AnonymousNumbers(adapter, str(status.stream_id)) if getattr(adapter, "anonymous_ws", True) else None

    def __aiter__(self):
        return self._gen()

    async def aclose(self) -> None:
        self._closed = True
        if self.anon:
            await self.anon.close()

    async def _page(self, **params) -> list[dict]:
        code, body = await self.a._get(f"/movies/{self.movie_id}/comments", {"limit": PAGE, **params})
        if code == 404:
            return []
        if code != 200 or body is None:
            raise RuntimeError(f"comments API error: HTTP {code}")
        items = body.get("comments")
        return [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []

    async def _gen(self):
        if self.anon:
            self.anon.start()
        first = True
        while not self._closed:
            if first or self.last_id < 1:  # slice_id は 1 以上のみ有効（0件の間は offset 取得を続ける）
                items = await self._page(offset=0)
            else:
                items = await self._page(slice_id=self.last_id)
                # 飽和（50件）なら境界まで offset で遡る。ページ重複/進捗なしで無限ループしない
                if len(items) >= PAGE:
                    seen = {c["id"] for c in items if "id" in c}
                    offset, pages = PAGE, 0
                    while pages < MAX_BACKFILL_PAGES:
                        more = await self._page(offset=offset)
                        fresh = [c for c in more if c.get("id") is not None and int(c["id"]) > self.last_id and c["id"] not in seen]
                        if not fresh:
                            break
                        items += fresh
                        seen |= {c["id"] for c in fresh}
                        if len(more) < PAGE or any(int(c["id"]) <= self.last_id for c in more if c.get("id") is not None):
                            break
                        offset += PAGE
                        pages += 1
                    else:
                        self.unreached += 1
                        log.warning("コメントの遡り取得が上限に達しました（未到達範囲あり）")
            first = False
            newest = max((int(c["id"]) for c in items if c.get("id") is not None), default=self.last_id)
            for c in sorted(items, key=lambda c: int(c.get("id", 0))):
                ev = to_comment(c)
                if ev is not None:  # 境界IDの再取得はDBの一意制約で重複排除される
                    if ev.is_anonymous and self.anon is not None and ev.platform_comment_id:
                        # 共通コメントIDで照合できた番号だけを付ける（できなければNULL）
                        ev.anonymous_label = await self.anon.wait_for(ev.platform_comment_id, 2.0)
                    yield ev
            self.last_id = max(self.last_id, newest)
            await asyncio.sleep(max(5.0, float(self.a.poll_seconds())))
