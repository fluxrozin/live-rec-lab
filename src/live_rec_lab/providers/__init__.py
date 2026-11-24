"""Normalize service responses before they enter the collection scheduler."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx


class LiveState(StrEnum):
    LIVE = "live"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    ERROR = "error"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class LiveObservation:
    state: LiveState
    live_id: str | None = None
    title: str | None = None
    started_at: datetime | None = None
    reason: str | None = None
    service: str | None = None
    account: str | None = None


@dataclass(frozen=True)
class ProviderComment:
    source_id: str
    body: str
    posted_at: datetime | None
    author_id: str | None
    author_name: str | None
    anonymous: bool = False
    anonymous_number: int | None = None


@dataclass(frozen=True)
class CommentPage:
    comments: tuple[ProviderComment, ...]
    total_count: int | None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _numeric_id(value: Any) -> str | None:
    text = str(value) if type(value) in (str, int) else ""
    return text if text.isascii() and text.isdecimal() and len(text) <= 30 and text.lstrip("0") else None


def _utc_seconds(value: Any) -> datetime | None:
    if type(value) is int and 0 < value <= 253402300799:
        return datetime.fromtimestamp(value, UTC)
    return None


def _http_reason(code: int) -> str | None:
    if code == 200:
        return None
    if code in (401, 403):
        return "access_denied"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "upstream_error"
    if code == 404:
        return "not_found"
    return "unexpected_response"


def _object_response(response: httpx.Response) -> dict[str, Any]:
    reason = _http_reason(response.status_code)
    if reason:
        raise ValueError(reason)
    try:
        payload = response.json()
    except ValueError:
        raise ValueError("invalid_response") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid_response")
    return payload


def parse_tiktok_room(response: httpx.Response) -> LiveObservation:
    try:
        payload = _object_response(response)
    except ValueError as error:
        return LiveObservation(LiveState.ERROR, reason=str(error))
    data = _mapping(payload.get("data"))
    live = _mapping(data.get("liveRoom"))
    user = _mapping(data.get("user"))
    status = live.get("status")
    if type(status) is not int or status not in (2, 4):
        return LiveObservation(LiveState.UNKNOWN, reason="unconfirmed_room_status")
    live_id = _numeric_id(user.get("roomId"))
    if status == 4:
        return LiveObservation(LiveState.OFFLINE, live_id=live_id)
    if not live_id:
        return LiveObservation(LiveState.UNKNOWN, reason="missing_room_id")
    return LiveObservation(LiveState.LIVE, live_id=live_id, title=_text(live.get("title")))


def parse_twitcasting_live(response: httpx.Response, target: str) -> LiveObservation:
    try:
        payload = _object_response(response)
    except ValueError as error:
        state = LiveState.UNKNOWN if str(error) == "not_found" else LiveState.ERROR
        return LiveObservation(state, reason=str(error))
    movie = _mapping(payload.get("movie"))
    broadcaster = _mapping(payload.get("broadcaster"))
    if (
        target not in (_text(broadcaster.get("id")), _text(broadcaster.get("screen_id")))
        or not _numeric_id(broadcaster.get("id"))
        or _numeric_id(movie.get("user_id")) != _numeric_id(broadcaster.get("id"))
    ):
        return LiveObservation(LiveState.UNKNOWN, reason="broadcaster_mismatch")
    live_id = _numeric_id(movie.get("id"))
    if movie.get("is_live") is False:
        return LiveObservation(LiveState.OFFLINE, live_id=live_id)
    if movie.get("is_live") is not True or not live_id:
        return LiveObservation(LiveState.UNKNOWN, reason="unconfirmed_movie_status")
    restrictions = tuple(movie.get(key) for key in ("is_protected", "is_membership", "is_premier"))
    if any(restriction is True for restriction in restrictions):
        return LiveObservation(LiveState.RESTRICTED, live_id=live_id, reason="restricted_movie")
    if any(restriction is not False for restriction in restrictions):
        return LiveObservation(LiveState.UNKNOWN, reason="unconfirmed_access")
    return LiveObservation(
        LiveState.LIVE,
        live_id=live_id,
        title=_text(movie.get("title")),
        started_at=_utc_seconds(movie.get("created")),
        service="twitcasting",
        account=target,
    )


def parse_twitcasting_comments(response: httpx.Response, movie_id: str) -> CommentPage:
    payload = _object_response(response)
    if _numeric_id(payload.get("movie_id")) != movie_id:
        raise ValueError("movie_mismatch")
    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list):
        raise ValueError("invalid_comment_page")
    comments = []
    for raw in raw_comments:
        item = _mapping(raw)
        source_id = _numeric_id(item.get("id"))
        if not source_id or not isinstance(item.get("message"), str):
            raise ValueError("invalid_comment")
        user = _mapping(item.get("from_user"))
        author_id = _text(user.get("screen_id"))
        anonymous = author_id == "c:tw1"
        comments.append(
            ProviderComment(
                source_id=source_id,
                body=item["message"],
                posted_at=_utc_seconds(item.get("created")),
                author_id=None if anonymous else author_id,
                author_name=None if anonymous else _text(user.get("name")),
                anonymous=anonymous,
            )
        )
    total = payload.get("all_count")
    return CommentPage(tuple(comments), total if type(total) is int and total >= 0 else None)
