"""公式 HLS からプリロールを除き、必要なときだけローカルのプレイリストを出す。"""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import httpx

from live_rec_lab.platforms.twitcasting import TWITCASTING_REFERER, TWITCASTING_USER_AGENT

_PLAYER_HEADERS = {
    "User-Agent": TWITCASTING_USER_AGENT,
    "Referer": TWITCASTING_REFERER,
    "Accept": "*/*",
}
_GLOBAL_EXACT = {"#EXTM3U", "#EXT-X-INDEPENDENT-SEGMENTS"}
_GLOBAL_PREFIXES = (
    "#EXT-X-VERSION:",
    "#EXT-X-TARGETDURATION:",
    "#EXT-X-MEDIA-SEQUENCE:",
    "#EXT-X-PLAYLIST-TYPE:",
    "#EXT-X-START:",
    "#EXT-X-ALLOW-CACHE:",
    "#EXT-X-DISCONTINUITY-SEQUENCE:",
    "#EXT-X-I-FRAMES-ONLY",
)
_MAP_URI = re.compile(r'URI="([^"]*)"', re.IGNORECASE)
_BANDWIDTH = re.compile(r"BANDWIDTH=(\d+)")
_EXTINF = re.compile(r"#EXTINF:([0-9.]+)", re.IGNORECASE)
_REFRESH_SECONDS = 1.0
_MAX_PLAYLIST_BYTES = 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}


class HlsPlaylistError(Exception):
    pass


@dataclass(frozen=True)
class FilteredPlaylist:
    text: str
    kept_segments: int
    dropped_preroll: int
    removed: bool
    kept_duration: float


def allowed_media_url(url: str | None) -> str | None:
    """録画に残せるのは twitcasting.tv とそのサブドメインだけ。"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    ports = {"http": 80, "https": 443}
    if parsed.scheme not in ports or port not in (None, ports[parsed.scheme]):
        return None
    if parsed.username is not None or parsed.fragment or any(ord(char) < 32 for char in url):
        return None
    if host != "twitcasting.tv" and not host.endswith(".twitcasting.tv"):
        return None
    return url


def _is_preroll(uri: str) -> bool:
    return urlparse(uri).path.rsplit("/", 1)[-1].startswith("preroll-")


def _is_discontinuity(line: str) -> bool:
    return line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE")


def _is_global(line: str) -> bool:
    return line in _GLOBAL_EXACT or line.startswith(_GLOBAL_PREFIXES)


def _rewrite_tag(line: str, base_url: str) -> str | None:
    match = _MAP_URI.search(line)
    if match is None:
        return line
    if not line.startswith("#EXT-X-MAP:"):
        return None
    absolute = urljoin(base_url, match.group(1))
    if _is_preroll(absolute) or allowed_media_url(absolute) is None:
        return None
    return f"{line[: match.start(1)]}{absolute}{line[match.end(1) :]}"


def filter_preroll_playlist(text: str, base_url: str) -> FilteredPlaylist:
    """明示キューまたは名前と切替境界を伴う先頭のプリロールを除く。"""
    header: list[str] = []
    pending: list[str] = []
    pieces: list[tuple[list[str], str]] = []
    tail: list[str] = []
    sequence: int | None = None
    sequence_index: int | None = None

    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            tail.append(line)
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                sequence = None
            sequence_index = len(header)
            header.append(line)
            continue
        if line.startswith("#") and _is_global(line):
            header.append(line)
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        pieces.append((pending, line))
        pending = []

    cut = next(
        (index for index, (tags, _) in enumerate(pieces) if any(
            _is_discontinuity(tag) or tag == "#EXT-X-CUE-IN" for tag in tags
        )),
        None,
    )
    if cut and not (
        all(_is_preroll(uri) for _, uri in pieces[:cut])
        or any(tag.startswith("#EXT-X-CUE-OUT:") for tag in pieces[0][0])
    ):
        cut = None
    body: list[str] = []
    dropped_prefix = 0
    dropped_preroll = 0
    kept = 0
    kept_duration = 0.0
    started = False
    removed = False

    for index, (tags, uri) in enumerate(pieces):
        absolute = urljoin(base_url, uri)
        leading = cut is not None and cut > 0 and index < cut
        blocked = allowed_media_url(absolute) is None
        if leading or blocked:
            if leading:
                dropped_preroll += 1
            if not started:
                dropped_prefix += 1
            removed = True
            continue
        for line in tags:
            if cut is not None and cut > 0 and index == cut and _is_discontinuity(line):
                removed = True
                continue
            rewritten = _rewrite_tag(line, base_url)
            if rewritten is not None:
                body.append(rewritten)
                match = _EXTINF.match(rewritten)
                if match is not None:
                    kept_duration += float(match.group(1))
        body.append(absolute)
        kept += 1
        started = True

    if dropped_prefix:
        updated = dropped_prefix if sequence is None else sequence + dropped_prefix
        tag = f"#EXT-X-MEDIA-SEQUENCE:{updated}"
        if sequence_index is None:
            header.append(tag)
        else:
            header[sequence_index] = tag
    rendered = "\n".join([*header, *body, *tail])
    if rendered:
        rendered += "\n"
    return FilteredPlaylist(rendered, kept, dropped_preroll, removed, kept_duration)


def pick_variant(text: str, base_url: str, quality: str) -> str | None:
    """マスタープレイリストから、許可されたホストの帯域を画質設定で選ぶ。"""
    found: list[tuple[int, str]] = []
    pending: int | None = None
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            match = _BANDWIDTH.search(line)
            pending = int(match.group(1)) if match else 0
            continue
        if not line or line.startswith("#") or pending is None:
            continue
        absolute = urljoin(base_url, line)
        if allowed_media_url(absolute):
            found.append((pending, absolute))
        pending = None
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1] if quality == "worst" else found[-1][1]


def _is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def _redirect_url(domain_url: str, location: str | None) -> str:
    target = allowed_media_url(urljoin(domain_url, location)) if location else None
    if target is None:
        raise HlsPlaylistError("許可外のプレイリスト転送先です")
    return target


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, _Server):
            self.send_error(500)
            return
        with server.lock:
            if self.path == server.playlist_path:
                payload = server.playlist.encode("utf-8")
                upstream = None
            else:
                payload = None
                upstream = server.resources.get(self.path)
        if payload is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif upstream is not None:
            self._send_resource(upstream)
        else:
            self.send_error(404)

    def _send_resource(self, upstream: str) -> None:
        headers = {**_PLAYER_HEADERS, "Accept-Encoding": "identity"}
        range_header = self.headers.get("Range")
        if range_header and re.fullmatch(r"bytes=[0-9]+-[0-9]*", range_header):
            headers["Range"] = range_header
        started = False
        try:
            with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                for _ in range(5):
                    with client.stream("GET", upstream, headers=headers) as response:
                        if response.status_code in _REDIRECTS:
                            upstream = _redirect_url(upstream, response.headers.get("Location"))
                            continue
                        if response.status_code not in {200, 206}:
                            break
                        self.send_response(response.status_code)
                        for name in ("Content-Type", "Content-Length", "Content-Range"):
                            if value := response.headers.get(name):
                                self.send_header(name, value)
                        self.end_headers()
                        started = True
                        for chunk in response.iter_raw():
                            self.wfile.write(chunk)
                        return
        except (HlsPlaylistError, httpx.HTTPError, OSError):
            if started:
                return
        self.send_error(502)

    def log_message(self, *_args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self, playlist: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.prefix = f"/{secrets.token_urlsafe(24)}"
        self.playlist_path = f"{self.prefix}/live.m3u8"
        self.resources: dict[str, str] = {}
        self.playlist = ""
        self.update(playlist)

    def update(self, playlist: str) -> None:
        def local_url(url: str) -> str:
            old = paths_by_url.get(url)
            if old:
                return old
            suffix = urlparse(url).path.rsplit(".", 1)[-1].lower()
            if suffix not in {"ts", "mp4", "m4s", "m4a", "aac", "webvtt", "vtt"}:
                suffix = "ts"
            path = f"{self.prefix}/{secrets.token_urlsafe(16)}.{suffix}"
            self.resources[path] = url
            paths_by_url[url] = path
            return path

        with self.lock:
            paths_by_url = {url: path for path, url in self.resources.items()}
            lines: list[str] = []
            for line in playlist.splitlines():
                if line.startswith("#EXT-X-MAP:"):
                    line = _MAP_URI.sub(lambda match: f'URI="{local_url(match.group(1))}"', line)
                elif line and not line.startswith("#"):
                    line = local_url(line)
                lines.append(line)
            self.playlist = "\n".join(lines) + "\n"
            # Keep a bounded queue of recently advertised resources for ffmpeg reloads.
            while len(self.resources) > 4096:
                del self.resources[next(iter(self.resources))]


class HlsMirror:
    """プリロールを除いたプレイリストをローカルに出し、配信中は更新する。"""

    def __init__(
        self,
        playlist_url: str,
        *,
        quality: str = "best",
        client: httpx.AsyncClient | None = None,
        wait_seconds: float = 20,
    ) -> None:
        self.upstream = playlist_url
        self.quality = quality
        self.wait_seconds = wait_seconds
        self._client = client
        self._owns_client = client is None
        self._closed = asyncio.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._refresh: asyncio.Task[None] | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
            self._owns_client = True
        return self._client

    async def _fetch(self, url: str) -> tuple[str, str]:
        if allowed_media_url(url) is None:
            raise HlsPlaylistError("許可外のプレイリストです")
        try:
            for _ in range(5):
                async with self._http().stream(
                    "GET", url, headers=_PLAYER_HEADERS, follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECTS:
                        url = _redirect_url(url, response.headers.get("Location"))
                        continue
                    if response.status_code != 200:
                        raise HlsPlaylistError(f"プレイリストの HTTP 応答 {response.status_code}")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > _MAX_PLAYLIST_BYTES:
                            raise HlsPlaylistError("プレイリストが読取上限を超えています")
                    return url, data.decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise HlsPlaylistError("プレイリストを取得できません") from exc
        raise HlsPlaylistError("プレイリストの転送回数が上限を超えています")

    async def _pull(self) -> FilteredPlaylist:
        final, text = await self._fetch(self.upstream)
        self.upstream = final
        if _is_master(text):
            variant = pick_variant(text, final, self.quality)
            if variant is None:
                raise HlsPlaylistError("プレイリストを取得できません")
            final, text = await self._fetch(variant)
            self.upstream = final
        if not text.lstrip("\ufeff \t\r\n").startswith("#EXTM3U"):
            raise HlsPlaylistError("プレイリストを取得できません")
        if any(
            line.lstrip().startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"))
            and not re.search(r"(:|,)METHOD=NONE(?:,|$)", line)
            for line in text.splitlines()
        ):
            raise HlsPlaylistError("暗号化されたプレイリストは録画できません")
        filtered = filter_preroll_playlist(text, self.upstream)
        if self._server is not None and (filtered.kept_segments > 0 or "#EXT-X-ENDLIST" in filtered.text):
            self._server.update(filtered.text)
        return filtered

    def _serve(self, playlist: str) -> str:
        server = _Server(playlist)
        thread = threading.Thread(target=server.serve_forever, name="hls-playlist", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        return f"http://{host}:{port}{server.playlist_path}"

    async def _refresh_loop(self) -> None:
        while not self._closed.is_set():
            try:
                await asyncio.wait_for(self._closed.wait(), _REFRESH_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                await self._pull()
            except (HlsPlaylistError, httpx.HTTPError):
                continue

    async def ffmpeg_input(self, stop: asyncio.Event) -> str | None:
        """配信本編候補を待ち、検証したプレイリストだけを録画子プロセスへ供給する。"""
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        while not stop.is_set():
            filtered = await self._pull()
            parts = [line for line in filtered.text.splitlines() if line and not line.startswith("#")]
            if parts and not all(_is_preroll(part) for part in parts):
                local = self._serve(filtered.text)
                self._refresh = asyncio.create_task(self._refresh_loop())
                return local
            if asyncio.get_running_loop().time() >= deadline:
                raise HlsPlaylistError("プレイリストに本番のセグメントがありません")
            try:
                await asyncio.wait_for(stop.wait(), _REFRESH_SECONDS)
            except TimeoutError:
                continue
        return None

    async def aclose(self) -> None:
        self._closed.set()
        task = self._refresh
        self._refresh = None
        if task is not None:
            task.cancel()
        server = self._server
        self._server = None
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(2)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
