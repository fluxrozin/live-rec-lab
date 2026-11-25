"""HTTP防御（Host/Origin/CSRF/ヘッダー/本文上限）。成功・失敗応答とも共通ヘッダーを付与。"""

from __future__ import annotations

import secrets

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY = 1024 * 1024
MAX_QUERY = 8 * 1024


def apply_headers(headers) -> None:
    headers["Content-Security-Policy"] = CSP
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Cross-Origin-Resource-Policy"] = "same-origin"
    headers["Cache-Control"] = "no-store"


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, port: int, csrf_token: str) -> None:
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.csrf = csrf_token

    def _deny(self, status: int, code: str, message: str) -> Response:
        r = JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
        apply_headers(r.headers)
        return r

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = scope["method"]
        deny: Response | None = None
        if headers.get("host", "") not in self.hosts:
            deny = self._deny(403, "FORBIDDEN_HOST", "許可されていないHostです")
        elif len(scope.get("query_string", b"")) > MAX_QUERY:
            deny = self._deny(414, "QUERY_TOO_LONG", "クエリが長すぎます")
        elif method not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is None or origin not in self.origins:
                deny = self._deny(403, "FORBIDDEN_ORIGIN", "Originが許可されていません")
            elif not secrets.compare_digest(headers.get("x-csrf-token", ""), self.csrf):
                deny = self._deny(403, "CSRF", "CSRFトークンが不正です")
            elif int(headers.get("content-length", "0") or 0) > MAX_BODY:
                deny = self._deny(413, "BODY_TOO_LARGE", "本文が大きすぎます")
        if deny is not None:
            await deny(scope, receive, send)
            return

        async def send_wrapped(message) -> None:
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders

                apply_headers(MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, send_wrapped)
