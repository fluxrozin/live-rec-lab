"""ツイキャス公式 API v2。匿名番号は公式応答に無いので空のまま。"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from live_rec_lab.monitoring.scheduler import RateGate
from live_rec_lab.platforms.base import CommentBatch, CommentIn, Snapshot, request_error_detail, retry_timestamp

API_ROOT = "https://apiv2.twitcasting.tv"
TWITCASTING_USER_AGENT = "LiveRecorder (public TwitCasting HLS)"
TWITCASTING_REFERER = "https://twitcasting.tv/"
_PAGE_LIMIT = 50
_CATCH_UP_PAGES = 5


def _epoch(value: object) -> datetime | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        number = int(value)
        return datetime.fromtimestamp(number, tz=UTC) if number > 0 else None
    except (ValueError, OSError, OverflowError):
        return None


def _payload(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(response: httpx.Response) -> int | None:
    error = _payload(response).get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if type(code) is int else None


def _remaining(response: httpx.Response) -> int | None:
    raw = response.headers.get("X-RateLimit-Remaining", "")
    return int(raw) if raw.isascii() and raw.isdecimal() else None


def _positive_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    raw = str(value)
    return raw if raw.isascii() and raw.isdecimal() and raw.strip("0") else None


class TwitCastingAdapter:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient | None = None,
        gate: RateGate | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self.owns_client = client is None
        self.gate = gate or RateGate()

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def aclose(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def _request(
        self, path: str, *, priority: str, params: dict[str, str | int] | None = None,
    ) -> tuple[httpx.Response | None, str]:
        if not self.configured():
            return None, "credentials_missing"
        if not self.gate.acquire(priority):
            return None, "rate_limit"
        try:
            response = await self.client.get(
                f"{API_ROOT}{path}",
                headers={"X-Api-Version": "2.0", "Accept": "application/json"},
                auth=httpx.BasicAuth(self.client_id, self.client_secret),
                params=params,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            return None, request_error_detail(exc)
        reset = retry_timestamp(response.headers, time.time())
        if response.status_code == 429 or (response.status_code == 403 and _error_code(response) == 2000):
            self.gate.note(0, reset)
            return response, "rate_limit"
        self.gate.note(_remaining(response), reset)
        if response.status_code in {401, 403}:
            return response, "authorization_denied"
        if response.status_code != 200:
            return response, f"http_{response.status_code}"
        return response, ""

    async def check(self, account: str, *, priority: str = "monitor") -> Snapshot:
        response, error = await self._request(f"/users/{quote(account, safe='')}/current_live", priority=priority)
        if error:
            if response is not None and response.status_code == 404 and _error_code(response) == 404:
                return Snapshot("OFFLINE")
            status = "ERROR" if error in {"authorization_denied", "credentials_missing"} else "UNKNOWN"
            return Snapshot(status, detail=error, retry_at=self.gate.reset_at if error == "rate_limit" else None)
        assert response is not None
        movie = _payload(response).get("movie")
        if not isinstance(movie, dict) or type(movie.get("is_live")) is not bool:
            return Snapshot("UNKNOWN", detail="invalid_movie")
        if movie["is_live"] is False:
            return Snapshot("OFFLINE")
        movie_id = _positive_id(movie.get("id"))
        if movie_id is None:
            return Snapshot("UNKNOWN", detail="missing_movie_id")
        title = movie.get("title")
        restricted = any(movie.get(flag) is True for flag in ("is_protected", "is_membership", "is_premier"))
        media = movie.get("hls_url")
        return Snapshot(
            "LIVE",
            platform_stream_id=movie_id,
            title=title[:200] if isinstance(title, str) and title else None,
            started_at=_epoch(movie.get("created")),
            media_url=media if not restricted and isinstance(media, str) else None,
            detail="restricted_movie" if restricted else "",
            restricted=restricted,
        )

    async def comments(self, account: str, stream_id: str, after_id: str | None) -> CommentBatch:
        del account
        if not _positive_id(stream_id):
            return CommentBatch(error="invalid_movie_id")
        path = f"/movies/{stream_id}/comments"
        cursor_id = _positive_id(after_id)
        cursor = int(cursor_id) if cursor_id else 0
        params: dict[str, str | int] = {"limit": _PAGE_LIMIT}
        if cursor_id:
            params["slice_id"] = cursor_id
        batch = CommentBatch(next_after_id=after_id, rate_remaining=self.gate.remaining)
        seen: set[str] = set()
        for page in range(_CATCH_UP_PAGES):
            if page:
                params = {"limit": _PAGE_LIMIT, "offset": page * _PAGE_LIMIT}
            response, error = await self._request(path, priority="comments", params=params)
            batch.rate_remaining = self.gate.remaining
            if error:
                batch.error = error
                batch.retry_at = self.gate.reset_at if error == "rate_limit" else None
                return batch
            assert response is not None
            payload = _payload(response)
            if "movie_id" in payload and _positive_id(payload["movie_id"]) != stream_id:
                batch.error = "comment_movie_mismatch"
                return batch
            rows = payload.get("comments")
            if not isinstance(rows, list):
                batch.error = "invalid_comments"
                return batch
            boundary_found = False
            received_at = datetime.now(UTC)
            for row in rows:
                comment_id = _positive_id(row.get("id")) if isinstance(row, dict) else None
                if comment_id is None:
                    batch.gap = "invalid_comment_row"
                    continue
                if int(comment_id) <= cursor:
                    boundary_found = True
                    continue
                if comment_id in seen:
                    continue
                seen.add(comment_id)
                message = row.get("message")
                if not isinstance(message, str) or not message:
                    batch.gap = "invalid_comment_row"
                    continue
                user = row.get("from_user")
                user = user if isinstance(user, dict) else {}
                user_id = user.get("id")
                name = user.get("name")
                batch.items.append(
                    CommentIn(
                        message=message,
                        platform_comment_id=comment_id,
                        platform_user_id=user_id if isinstance(user_id, str) and user_id != "c:tw1" else None,
                        nickname=name if isinstance(name, str) else None,
                        posted_at=_epoch(row.get("created")),
                        received_at=received_at,
                    )
                )
            total = payload.get("all_count")
            end_of_page = type(total) is int and page * _PAGE_LIMIT + len(rows) >= total
            if boundary_found or not rows or end_of_page or (not page and len(rows) < _PAGE_LIMIT):
                break
        else:
            batch.gap = batch.gap or "comment_history_limit"
        if seen:
            batch.next_after_id = max(seen, key=int)
        return batch
