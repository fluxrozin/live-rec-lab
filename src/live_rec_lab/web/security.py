"""ループバック専用Webと変更要求の検証。"""

from __future__ import annotations

import re
import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOOPBACK_AUTHORITY = re.compile(r"(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", re.IGNORECASE)
_FORWARDED = frozenset({"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def tokens_match(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return supplied.isascii() and secrets.compare_digest(expected, supplied)


def _authority(value: str) -> tuple[str, int] | None:
    match = _LOOPBACK_AUTHORITY.fullmatch(value)
    if match is None:
        return None
    port = int(match[2]) if match[2] else 80
    return (match[1].lower(), port) if 1 <= port <= 65535 else None


class LocalSecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.csrf = secrets.token_urlsafe(32)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        hosts = request.headers.getlist("host")
        host = _authority(hosts[0]) if len(hosts) == 1 else None
        if host is None or any(header in request.headers for header in _FORWARDED):
            await self._reject(scope, receive, send, "ループバックの正しい Host が必要です")
            return
        if request.method not in _SAFE_METHODS:
            origins = request.headers.getlist("origin")
            origin_matches = (
                len(origins) == 1
                and origins[0].startswith("http://")
                and _authority(origins[0].removeprefix("http://")) == host
            )
            site = request.headers.get("sec-fetch-site")
            if (origins and not origin_matches) or site not in {None, "none", "same-origin"}:
                await self._reject(scope, receive, send, "別のオリジンからの変更は拒否しました")
                return
        request.state.csrf = self.csrf

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers["x-frame-options"] = "DENY"
                headers["x-content-type-options"] = "nosniff"
                # Chromium sends Origin: null for same-origin form navigations with no-referrer.
                headers["referrer-policy"] = "same-origin"
                if not request.url.path.startswith("/static/"):
                    headers["cache-control"] = "no-store"
                    if request.method == "GET" and 200 <= message["status"] < 300:
                        headers["x-csrf-token"] = self.csrf
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, message: str) -> None:
        response = JSONResponse({"error": {"code": "forbidden", "message": message}}, status_code=403)
        await response(scope, receive, send)
