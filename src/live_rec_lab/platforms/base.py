"""アダプタが返す値。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from socket import gaierror

import httpx


def request_error_detail(error: httpx.HTTPError) -> str:
    """監視イベント向けに、例外の本文を含まない固定コードを返す。"""
    match error:
        case httpx.ConnectError():
            cause = error.__cause__
            while cause is not None:
                if isinstance(cause, gaierror):
                    return "dns_error"
                cause = cause.__cause__
            return "connect_error"
        case httpx.TimeoutException():
            return "timeout"
        case httpx.NetworkError():
            return "network_error"
        case httpx.ProtocolError():
            return "protocol_error"
        case httpx.ProxyError():
            return "proxy_error"
        case httpx.DecodingError():
            return "invalid_encoding"
        case _:
            return "request_error"


@dataclass
class Snapshot:
    status: str
    platform_stream_id: str | None = None
    title: str | None = None
    started_at: datetime | None = None
    media_url: str | None = None
    detail: str = ""
    restricted: bool = False
    ended_at: datetime | None = None
    retry_at: float | None = field(default=None, compare=False)
    paused: bool = field(default=False, compare=False)


@dataclass
class CommentIn:
    message: str
    platform_comment_id: str | None = None
    platform_user_id: str | None = None
    nickname: str | None = None
    posted_at: datetime | None = None
    is_anonymous: bool | None = None
    anonymous_label: str | None = None
    source: str = "api"
    received_at: datetime | None = None


@dataclass
class CommentBatch:
    items: list[CommentIn] = field(default_factory=list)
    next_after_id: str | None = None
    rate_remaining: int | None = None
    error: str = ""
    gap: str = ""
    retry_at: float | None = None


def retry_timestamp(headers: httpx.Headers, now: float) -> float | None:
    """HTTP Retry-After または公開されている残枠のリセット時刻を読む。"""
    retry = headers.get("Retry-After")
    if retry:
        if retry.isascii() and retry.isdecimal():
            return now + int(retry)
        try:
            moment = parsedate_to_datetime(retry).timestamp()
        except (TypeError, ValueError, OverflowError):
            moment = 0
        if moment > now:
            return moment
    for header in ("X-RateLimit-Reset", "RateLimit-Reset"):
        reset = headers.get(header, "")
        if reset.isascii() and reset.isdecimal() and int(reset) > now:
            return float(int(reset))
    return None
