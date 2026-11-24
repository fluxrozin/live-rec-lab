"""公式 HLS からプリロールを除き、必要なときだけローカルのプレイリストを出す。"""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
from dataclasses import dataclass, replace
from hashlib import sha256
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
_BYTERANGE = re.compile(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?")
_REFRESH_SECONDS = 1.0
_MAX_PLAYLIST_BYTES = 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}
_SUCCESSFUL_RESOURCE_RESULTS = {"200", "206", "client_closed"}


class HlsPlaylistError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FilteredPlaylist:
    text: str
    kept_segments: int
    dropped_preroll: int
    removed: bool
    kept_duration: float
    media_sequence: int
    endlist: bool
    only_preroll: bool
    last_segment: str | None

    def finite_identity(self) -> str | None:
        if not self.endlist or not self.kept_segments:
            return None
        # Signed URLs can change while referring to the same finite media.
        parts = [str(self.media_sequence)]
        for line in self.text.splitlines():
            if line.startswith("#EXT-X-MAP:"):
                parts.append(_MAP_URI.sub(lambda match: urlparse(match.group(1)).path, line))
            elif line.startswith(("#EXTINF:", "#EXT-X-BYTERANGE:")):
                parts.append(line)
            elif line and not line.startswith("#"):
                parts.append(urlparse(line).path)
        return sha256("\n".join(parts).encode()).hexdigest()

    def initialization(self) -> str | None:
        for line in self.text.splitlines():
            if line.startswith("#EXT-X-MAP:"):
                return _map_identity(line)
            if line and not line.startswith("#"):
                break
        return None


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


def _is_global(line: str) -> bool:
    return line in _GLOBAL_EXACT or line.startswith(_GLOBAL_PREFIXES)


def _rewrite_tag(line: str, base_url: str) -> str | None:
    match = _MAP_URI.search(line)
    if match is None:
        return line
    if not line.startswith("#EXT-X-MAP:"):
        return None
    absolute = urljoin(base_url, match.group(1))
    if allowed_media_url(absolute) is None:
        return None
    return f"{line[: match.start(1)]}{absolute}{line[match.end(1) :]}"


def _initialization_uris(tags: list[str]) -> list[str]:
    return [match.group(1) for line in tags if line.startswith("#EXT-X-MAP:") and (match := _MAP_URI.search(line))]


def _preroll_boundary(pieces: list[tuple[list[str], str]], base_url: str) -> int | None:
    if pieces and any(tag.startswith("#EXT-X-CUE-OUT:") for tag in pieces[0][0]):
        cue_in = next((index for index, (tags, _) in enumerate(pieces[1:], 1) if "#EXT-X-CUE-IN" in tags), None)
        if cue_in is not None:
            return cue_in

    active_map = None
    for index, (tags, uri) in enumerate(pieces):
        maps = _initialization_uris(tags)
        if index and not _is_preroll(uri) and allowed_media_url(urljoin(base_url, uri)) is not None:
            # A retained segment must either bring its own program initialization
            # or come from a playlist that had no initialization to carry over.
            if maps:
                new_map = maps[-1]
                if not _is_preroll(new_map) and allowed_media_url(urljoin(base_url, new_map)) is not None:
                    return index
            elif active_map is None:
                return index
        if maps:
            active_map = maps[-1]
        if not _is_preroll(uri) and (active_map is None or not _is_preroll(active_map)):
            break
    return None


def filter_preroll_playlist(text: str, base_url: str) -> FilteredPlaylist:
    """明示キュー、またはメディアや初期化ファイルと切替境界で識別したプリロールを除く。"""
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

    cut = _preroll_boundary(pieces, base_url)
    if cut is not None and any(
        (match := _BYTERANGE.fullmatch(tag)) and match[2] is None for tag in pieces[cut][0]
    ):
        # The initialization split can carry the previous byte offset forward.
        cut = None
    body: list[str] = []
    dropped_prefix = 0
    dropped_preroll = 0
    kept = 0
    kept_duration = 0.0
    last_segment = None
    only_preroll = True
    preroll_map = False
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
            if index == cut and line == "#EXT-X-DISCONTINUITY":
                removed = True
                continue
            rewritten = _rewrite_tag(line, base_url)
            if rewritten is not None:
                if rewritten.startswith("#EXT-X-MAP:") and (match := _MAP_URI.search(rewritten)):
                    preroll_map = _is_preroll(match.group(1))
                body.append(rewritten)
                match = _EXTINF.match(rewritten)
                if match is not None:
                    kept_duration += float(match.group(1))
        body.append(absolute)
        last_segment = urlparse(absolute).path
        only_preroll &= _is_preroll(absolute) or preroll_map
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
    return FilteredPlaylist(
        rendered, kept, dropped_preroll, removed, kept_duration, (sequence or 0) + dropped_prefix,
        bool(tail), only_preroll, last_segment,
    )


def _map_identity(line: str) -> str:
    return _MAP_URI.sub(lambda match: urlparse(match.group(1)).path, line)


def _preserve_first_range(prefix: list[str], tail: list[str]) -> None:
    first_uri = next((index for index, line in enumerate(tail) if line and not line.startswith("#")), None)
    if first_uri is None or tail[first_uri] != prefix[-1]:
        return
    first_range = next((index for index in range(first_uri) if _BYTERANGE.fullmatch(tail[index])), None)
    if first_range is None or "@" in tail[first_range]:
        return
    end = None
    pending = None
    previous_uri = None
    for line in prefix:
        match = _BYTERANGE.fullmatch(line)
        if match:
            pending = match
        elif line and not line.startswith("#"):
            offset = int(pending[2]) if pending and pending[2] is not None else end if line == previous_uri else None
            end = offset + int(pending[1]) if offset is not None and pending else None
            previous_uri = line
            pending = None
    if end is not None:
        tail[first_range] += f"@{end}"


def _split_initialization(playlist: FilteredPlaylist) -> tuple[FilteredPlaylist, FilteredPlaylist | None]:
    """同じFFmpeg入力に別の初期化情報を混ぜない。"""
    lines = playlist.text.splitlines()
    initialization = None
    segment_start = 0
    segments = 0
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MAP:"):
            new_initialization = _map_identity(line)
            if segments and new_initialization != initialization:
                break
            initialization = new_initialization
        elif line and not line.startswith("#"):
            segments += 1
            segment_start = index + 1
    else:
        return playlist, None

    # Media tags between the last URI and the new map belong to the next part.
    prefix_lines = lines[:segment_start]
    header = [
        tag for tag in prefix_lines if _is_global(tag)
        and not tag.startswith(("#EXT-X-MEDIA-SEQUENCE:", "#EXT-X-START:", "#EXT-X-DISCONTINUITY-SEQUENCE:"))
    ]
    discontinuities = prefix_lines.count("#EXT-X-DISCONTINUITY")
    for tag in prefix_lines:
        if tag.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            value = tag.rsplit(":", 1)[1]
            discontinuities += int(value) if value.isdecimal() else 0
            break
    header.append(f"#EXT-X-MEDIA-SEQUENCE:{playlist.media_sequence + segments}")
    if discontinuities:
        header.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuities}")
    tail_lines = lines[segment_start:]
    _preserve_first_range(prefix_lines, tail_lines)
    prefix_lines = [line for line in prefix_lines if not line.startswith("#EXT-X-START:")]
    prefix = filter_preroll_playlist("\n".join([*prefix_lines, "#EXT-X-ENDLIST"]), "")
    tail = filter_preroll_playlist("\n".join([*header, *tail_lines]), "")
    return replace(prefix, dropped_preroll=playlist.dropped_preroll), tail


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
            kind, url = upstream
            result = self._send_resource(url)
            with server.lock:
                counts = server.resource_results.setdefault(kind, {})
                counts[result] = counts.get(result, 0) + 1
        else:
            self.send_error(404)

    def _send_resource(self, upstream: str) -> str:
        headers = {**_PLAYER_HEADERS, "Accept-Encoding": "identity"}
        range_header = self.headers.get("Range")
        if range_header and re.fullmatch(r"bytes=[0-9]+-[0-9]*", range_header):
            headers["Range"] = range_header
        started = False
        result = "redirect_limit"
        try:
            with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                for _ in range(5):
                    with client.stream("GET", upstream, headers=headers) as response:
                        if response.status_code in _REDIRECTS:
                            upstream = _redirect_url(upstream, response.headers.get("Location"))
                            continue
                        result = str(response.status_code)
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
                        return result
        except HlsPlaylistError:
            result = "rejected_redirect"
        except httpx.HTTPError:
            result = "body_error" if started else "connection_error"
            if started:
                return result
        except OSError:
            return "client_closed"
        self.send_error(502)
        return result

    def log_message(self, *_args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self, playlist: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.prefix = f"/{secrets.token_urlsafe(24)}"
        self.playlist_path = f"{self.prefix}/live.m3u8"
        self.resources: dict[str, tuple[str, str]] = {}
        self.resource_results: dict[str, dict[str, int]] = {}
        self.playlist = ""
        self.update(playlist)

    def update(self, playlist: str) -> None:
        def local_url(kind: str, url: str) -> str:
            resource = (kind, url)
            old = paths_by_resource.get(resource)
            if old:
                return old
            suffix = urlparse(url).path.rsplit(".", 1)[-1].lower()
            if suffix not in {"ts", "mp4", "m4s", "m4a", "aac", "webvtt", "vtt"}:
                suffix = "ts"
            path = f"{self.prefix}/{secrets.token_urlsafe(16)}.{suffix}"
            self.resources[path] = resource
            paths_by_resource[resource] = path
            return path

        with self.lock:
            paths_by_resource = {resource: path for path, resource in self.resources.items()}
            lines: list[str] = []
            for line in playlist.splitlines():
                if line.startswith("#EXT-X-MAP:"):
                    line = _MAP_URI.sub(lambda match: f'URI="{local_url("init", match.group(1))}"', line)
                elif line and not line.startswith("#"):
                    line = local_url("media", line)
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
        self._media_url: str | None = None
        self._published: FilteredPlaylist | None = None
        self._progressed = False
        self._identified_main = False
        self.unconfirmed = False
        self.end_confirmed = False
        self.last_error: HlsPlaylistError | None = None
        self._resource_results: dict[str, dict[str, int]] = {}
        self.next_playlist: FilteredPlaylist | None = None
        self._sequence_floor: int | None = None

    def diagnostics(self) -> dict:
        playlist = self._published
        tags = playlist.text.splitlines() if playlist else []
        return {
            "http_status": self.last_error.status_code if self.last_error else None,
            "playlist_advanced": self._progressed,
            "segments": playlist.kept_segments if playlist else 0,
            "media_sequence": playlist.media_sequence if playlist else None,
            "discontinuities": tags.count("#EXT-X-DISCONTINUITY"),
            "init_sections": sum(line.startswith("#EXT-X-MAP:") for line in tags),
            "endlist": playlist.endlist if playlist else None,
            "end_confirmed": self.end_confirmed,
            "resource_results": self._resource_results,
        }

    def resource_failed(self) -> bool:
        return any(results.keys() - _SUCCESSFUL_RESOURCE_RESULTS for results in self._resource_results.values())

    def finite_identity(self) -> str | None:
        return self._published.finite_identity() if self._published is not None else None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
            self._owns_client = True
        return self._client

    async def _fetch(self, url: str, *, stage: str) -> tuple[str, str]:
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
                        raise HlsPlaylistError(
                            f"{stage}プレイリストの HTTP 応答 {response.status_code}",
                            status_code=response.status_code,
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > _MAX_PLAYLIST_BYTES:
                            raise HlsPlaylistError("プレイリストが読取上限を超えています")
                    return url, data.decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise HlsPlaylistError("プレイリストを取得できません") from exc
        raise HlsPlaylistError("プレイリストの転送回数が上限を超えています")

    async def _resolve(self) -> tuple[str, str]:
        final, text = await self._fetch(self.upstream, stage="起点")
        if not _is_master(text):
            return final, text
        variant = pick_variant(text, final, self.quality)
        if variant is None:
            raise HlsPlaylistError("利用可能なメディアプレイリストがありません")
        return await self._fetch(variant, stage="メディア")

    async def _pull(self) -> FilteredPlaylist:
        status_code = None
        try:
            if self._media_url:
                final, text = await self._fetch(self._media_url, stage="メディア")
            else:
                final, text = await self._resolve()
        except HlsPlaylistError as exc:
            if self._media_url in (None, self.upstream) or exc.status_code != 404:
                raise
            status_code = exc.status_code
            final, text = await self._resolve()
        if not text.lstrip("\ufeff \t\r\n").startswith("#EXTM3U"):
            raise HlsPlaylistError("プレイリストを取得できません")
        if any(
            line.lstrip().startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"))
            and not re.search(r"(:|,)METHOD=NONE(?:,|$)", line)
            for line in text.splitlines()
        ):
            raise HlsPlaylistError("暗号化されたプレイリストは録画できません")
        previous_url = self._media_url
        playlist = filter_preroll_playlist(text, final)
        if self._published is not None and previous_url and (
            urlparse(previous_url).path != urlparse(final).path
            or playlist.media_sequence + playlist.kept_segments
            < self._published.media_sequence + self._published.kept_segments
        ) and playlist.kept_segments:
            # A new variant may restart its sequence. Give it a fresh ffmpeg input.
            raise HlsPlaylistError("メディアプレイリストが切り替わりました", status_code=status_code)
        self._media_url = final
        return playlist

    def _serve(self, playlist: str) -> str:
        server = _Server(playlist)
        thread = threading.Thread(target=server.serve_forever, name="hls-playlist", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        return f"http://{host}:{port}{server.playlist_path}"

    def _initialization_window(
        self, playlist: FilteredPlaylist,
    ) -> tuple[FilteredPlaylist, FilteredPlaylist, FilteredPlaylist | None]:
        current, following = _split_initialization(playlist)
        while (
            self._sequence_floor is not None and following is not None
            and current.media_sequence + current.kept_segments <= self._sequence_floor
        ):
            playlist = following
            current, following = _split_initialization(playlist)
        return playlist, current, following

    async def _refresh_loop(self) -> None:
        failures = 0
        while not self._closed.is_set():
            try:
                await asyncio.wait_for(self._closed.wait(), _REFRESH_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                playlist = await self._pull()
            except HlsPlaylistError as exc:
                self.last_error = exc
                failures += 1
                if failures >= 2 or exc.status_code in {401, 403}:
                    # Let ffmpeg drain the published segments before reconnecting.
                    if self._server is not None and self._published is not None:
                        self._server.update(f"{self._published.text}#EXT-X-ENDLIST\n")
                    return
                continue
            self.last_error = None
            failures = 0
            previous = self._published
            if self._server is None or previous is None:
                return
            if playlist.kept_segments and not playlist.only_preroll:
                window, current, following = self._initialization_window(playlist)
                floor = self._sequence_floor
                if floor is not None and current.media_sequence + current.kept_segments <= floor:
                    continue
                if current.initialization() != previous.initialization():
                    self.next_playlist = window
                    self._server.update(f"{previous.text}#EXT-X-ENDLIST\n")
                    return
                playlist = current
                if following is not None:
                    self.next_playlist = following
                    self._published = current
                    self._server.update(current.text)
                    return
            if playlist.kept_segments and not playlist.only_preroll:
                self._progressed |= (
                    playlist.media_sequence + playlist.kept_segments > previous.media_sequence + previous.kept_segments
                    or playlist.last_segment != previous.last_segment
                )
                self._identified_main |= bool(playlist.dropped_preroll)
                self._published = playlist
                self._server.update(playlist.text)
            elif playlist.endlist:
                # Keep playable media if the upstream emits a terminal empty list.
                terminal = f"{previous.text}#EXT-X-ENDLIST\n"
                self._published = replace(previous, text=terminal, endlist=True)
                self._server.update(terminal)
            if playlist.endlist:
                self.end_confirmed = self._identified_main
                return

    async def ffmpeg_input(
        self, stop: asyncio.Event, *, captured_finite: set[str] | None = None, retry_finite: str | None = None,
        initial_playlist: FilteredPlaylist | None = None,
    ) -> str | None:
        """配信本編候補を待ち、検証したプレイリストだけを録画子プロセスへ供給する。"""
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        while not stop.is_set():
            try:
                playlist = initial_playlist if initial_playlist is not None else await self._pull()
            except HlsPlaylistError as exc:
                self.last_error = exc
                if exc.status_code != 404 or asyncio.get_running_loop().time() >= deadline:
                    raise
            else:
                self.last_error = None
                expired = asyncio.get_running_loop().time() >= deadline
                playlist, following = _split_initialization(playlist)
                skipped_preroll = False
                while playlist.only_preroll and following is not None:
                    playlist, following = _split_initialization(following)
                    skipped_preroll = True
                if playlist.kept_segments and not playlist.only_preroll:
                    confirmed = not playlist.endlist or bool(playlist.dropped_preroll) or skipped_preroll
                    finite = playlist.finite_identity()
                    unseen_finite = captured_finite is None or finite not in captured_finite
                    retry_now = retry_finite is not None and finite == retry_finite
                    if confirmed or following is not None or initial_playlist is not None or (
                        unseen_finite and (retry_now or expired)
                    ):
                        if stop.is_set():
                            return None
                        self._published = playlist
                        self.next_playlist = following
                        if initial_playlist is not None or skipped_preroll:
                            self._sequence_floor = playlist.media_sequence
                        self._identified_main = bool(playlist.dropped_preroll) or skipped_preroll
                        self.unconfirmed = not confirmed and following is None and initial_playlist is None
                        self.end_confirmed = playlist.endlist and confirmed and following is None
                        local = self._serve(playlist.text)
                        if not playlist.endlist:
                            self._refresh = asyncio.create_task(self._refresh_loop())
                        return local
                if expired:
                    raise HlsPlaylistError("HLSプレイリストに本編を確認できません")
                if playlist.endlist:
                    self._media_url = None
                initial_playlist = None
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(stop.wait(), min(_REFRESH_SECONDS, remaining))
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
            with server.lock:
                self._resource_results = {kind: counts.copy() for kind, counts in server.resource_results.items()}
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
