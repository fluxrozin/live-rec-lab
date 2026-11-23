"""TikTokLive の公開されている状態確認とコメント項目。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import httpx

from live_rec_lab.platforms.base import CommentIn, Snapshot, request_error_detail, retry_timestamp

Probe = Callable[[str], Awaitable[Snapshot]]
ROOM_URL = "https://www.tiktok.com/api-live/user/room/"


class CommentConnectionError(Exception):
    def __init__(self, code: str, *, retry_at: float | None = None, dropped: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.retry_at = retry_at
        self.dropped = dropped


def _positive_int(value: object) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and len(value) <= 24 and value.isascii() and value.isdecimal():
        return int(value) or None
    return None


def _epoch(value: object) -> datetime | None:
    number = _positive_int(value)
    if number is None:
        return None
    seconds = number / 1000 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def comment_from_event(event: object) -> CommentIn | None:
    content = getattr(event, "content", None) or getattr(event, "comment", None)
    if not isinstance(content, str) or not content:
        return None
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", None)
    user_id = getattr(user, "unique_id", None) or getattr(user, "display_id", None)
    common = getattr(event, "common", None)
    msg_id = _positive_int(getattr(common, "msg_id", None))
    return CommentIn(
        message=content,
        platform_comment_id=str(msg_id) if msg_id is not None else None,
        platform_user_id=user_id if isinstance(user_id, str) and user_id else None,
        nickname=nickname if isinstance(nickname, str) and nickname else None,
        posted_at=_epoch(getattr(common, "create_time", None)),
        source="websocket",
        received_at=datetime.now(UTC),
    )


def room_snapshot(status_code: int, body: object) -> Snapshot:
    """2 は配信中、4 は終了。1 と 3 は意味が分かるまで終了にしない。"""
    if status_code != 200:
        return Snapshot("ERROR", detail=f"http_{status_code}")
    if not isinstance(body, dict):
        return Snapshot("ERROR", detail="invalid_json")
    if body.get("message") == "user_not_found":
        return Snapshot("ERROR", detail="account_not_found")
    if body.get("statusCode") not in (None, 0, "0"):
        return Snapshot("ERROR", detail="api_rejected")
    data = body.get("data")
    room = data.get("liveRoom") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(room, dict):
        return Snapshot("ERROR", detail="invalid_json")
    code = _positive_int(room.get("status"))
    if code == 4:
        return Snapshot("OFFLINE")
    if code in {1, 3}:
        return Snapshot("UNKNOWN", detail=f"room_status_{code}")
    if code != 2:
        return Snapshot("ERROR", detail=f"room_status_{code}" if code is not None else "invalid_json")
    room_id = _room_id(data, room)
    if room_id is None:
        return Snapshot("UNKNOWN", detail="missing_room_id")
    title = room.get("title")
    return Snapshot(
        "LIVE",
        platform_stream_id=room_id,
        title=title[:200] if isinstance(title, str) and title else None,
        started_at=_epoch(room.get("createTime")),
    )


def _room_id(data: dict, room: dict) -> str | None:
    user = data.get("user")
    for source in (room, user):
        if isinstance(source, dict):
            for key in ("roomId", "room_id"):
                if value := _positive_int(source.get(key)):
                    return str(value)
    return None


def check_failure_pauses(detail: str) -> bool:
    if not detail.startswith("http_"):
        return detail in {"timeout", "connect_error", "dns_error", "network_error", "protocol_error"}
    status = detail.removeprefix("http_")
    return status.isdecimal() and (int(status) in {403, 429} or int(status) >= 500)


async def default_probe(account: str) -> Snapshot:
    from TikTokLive import TikTokLiveClient

    client = TikTokLiveClient(unique_id=account)
    try:
        try:
            response = await client.web.get(url=ROOM_URL, extra_params={"uniqueId": account, "sourceType": 54})
        except httpx.HTTPError as exc:
            return Snapshot("UNKNOWN", detail=request_error_detail(exc))
        if response.status_code != 200:
            snapshot = room_snapshot(response.status_code, None)
            snapshot.retry_at = retry_timestamp(response.headers, time.time())
            return snapshot
        try:
            body = response.json()
        except ValueError:
            return Snapshot("ERROR", detail="invalid_json")
        return room_snapshot(response.status_code, body)
    finally:
        await client.web.close()


class TikTokAdapter:
    def __init__(self, probe: Probe | None = None) -> None:
        self._probe = probe or default_probe
        self._blocked_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    async def check(self, account: str) -> Snapshot:
        now = time.time()
        blocked = self._blocked_until.get(account, 0)
        if now < blocked:
            return Snapshot("ERROR", detail="確認を一時停止しています", retry_at=blocked, paused=True)
        try:
            snapshot = await self._probe(account)
        except (ImportError, httpx.HTTPError):
            return Snapshot("UNKNOWN", detail="probe_unavailable")
        if check_failure_pauses(snapshot.detail):
            count = min(self._failures.get(account, 0) + 1, 7)
            self._failures[account] = count
            fallback_delay = min(2**count, 90) + random.random()
            snapshot.retry_at = max(snapshot.retry_at or 0, now + fallback_delay)
            self._blocked_until[account] = snapshot.retry_at
        elif snapshot.status in {"LIVE", "OFFLINE"}:
            self._failures.pop(account, None)
            self._blocked_until.pop(account, None)
        return snapshot

    async def stream_comments(
        self,
        account: str,
        stream_id: str,
        stop: asyncio.Event,
        *,
        on_connect: Callable[[], None],
    ) -> AsyncIterator[CommentIn]:
        from TikTokLive import TikTokLiveClient
        from TikTokLive.client.errors import AgeRestrictedError, SignAPIError, UserOfflineError, WebcastBlockedError
        from TikTokLive.events import CommentEvent, ConnectEvent

        room_id = _positive_int(stream_id)
        if room_id is None:
            raise CommentConnectionError("invalid_room_id")
        client = TikTokLiveClient(unique_id=account)
        queue: asyncio.Queue[CommentIn] = asyncio.Queue(maxsize=1000)
        dropped = 0
        overflow = asyncio.Event()
        mismatched_room = False

        @client.on(ConnectEvent)
        def connected(event: ConnectEvent) -> None:
            nonlocal mismatched_room
            if _positive_int(event.room_id) != room_id:
                mismatched_room = True
                overflow.set()
            else:
                on_connect()

        @client.on(CommentEvent)
        def received(event: CommentEvent) -> None:
            nonlocal dropped
            if mismatched_room or (item := comment_from_event(event)) is None:
                return
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                dropped += 1
                overflow.set()

        # SDK start() returns the receiver task after signing and setup. Keep both lifetimes distinct.
        starter = asyncio.create_task(client.start(room_id=room_id, fetch_live_check=False))
        stopping = asyncio.create_task(stop.wait())
        overrun = asyncio.create_task(overflow.wait())
        receiver: asyncio.Task | None = None
        next_item: asyncio.Task | None = None
        try:
            await asyncio.wait((starter, stopping), return_when=asyncio.FIRST_COMPLETED)
            if not starter.done():
                return
            receiver = starter.result()
            while not (receiver.done() or stopping.done() or overrun.done()):
                next_item = asyncio.create_task(queue.get())
                await asyncio.wait((receiver, stopping, overrun, next_item), return_when=asyncio.FIRST_COMPLETED)
                if next_item.done():
                    yield next_item.result()
                else:
                    next_item.cancel()
                    await asyncio.gather(next_item, return_exceptions=True)
                next_item = None
            if not receiver.done():
                await self._disconnect(client, receiver)
            while not queue.empty():
                yield queue.get_nowait()
            if mismatched_room:
                raise CommentConnectionError("comment_room_mismatch")
            if dropped:
                raise CommentConnectionError("comment_queue_full", dropped=dropped)
            if not receiver.cancelled():
                receiver.result()
        except UserOfflineError as exc:
            raise CommentConnectionError("comment_connection_not_ready") from exc
        except (AgeRestrictedError, WebcastBlockedError) as exc:
            raise CommentConnectionError("comment_access_restricted") from exc
        except SignAPIError as exc:
            response = exc.response
            retry_at = retry_timestamp(response.headers, time.time()) if response is not None else None
            code = "signature_rate_limit" if response is not None and response.status_code == 429 else "signature_error"
            raise CommentConnectionError(code, retry_at=retry_at) from exc
        finally:
            tasks = [task for task in (starter, stopping, overrun, next_item) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if receiver is None and starter.done() and not starter.cancelled() and starter.exception() is None:
                receiver = starter.result()
            try:
                await self._disconnect(client, receiver)
            finally:
                try:
                    await client.web.close()
                finally:
                    signer = getattr(client.web, "signer", None)
                    if signer is not None:
                        await signer.httpx_client.aclose()

    @staticmethod
    async def _disconnect(client, receiver: asyncio.Task | None) -> None:
        consumer = asyncio.current_task()
        if receiver is not None and consumer is not None and consumer.cancelling():
            receiver.cancel()
        disconnect = asyncio.create_task(client.disconnect())
        try:
            done, _ = await asyncio.wait((disconnect,), timeout=4)
            if not done:
                disconnect.cancel()
                if receiver is not None and not receiver.done():
                    receiver.cancel()
        finally:
            await asyncio.gather(disconnect, return_exceptions=True)
            if receiver is not None:
                await asyncio.gather(receiver, return_exceptions=True)
