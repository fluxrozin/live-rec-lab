"""Read APIs for the isolated collection workspace."""

import logging
import secrets
import sqlite3
from contextlib import closing
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.exceptions import HTTPException

from .. import streamers as registry
from ..db import connect
from ..isolation import FixtureWorkspace, IsolationError
from .pages import mount_pages
from .queries import comment_page, page_rows, read_database, session_select
from .speech_api import mount_speech_api

_LOG = logging.getLogger(__name__)
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
    ),
}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
Page = Annotated[int, Query(ge=1, le=10_000_000)]
PageSize = Annotated[int, Query(ge=1, le=200)]
SessionId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class StreamerEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: Annotated[str | None, Field(max_length=100)] = None
    monitoring_enabled: StrictBool
    recording_enabled: StrictBool
    comments_enabled: StrictBool

    def settings(self) -> registry.StreamerSettings:
        return registry.StreamerSettings(
            self.display_name,
            self.monitoring_enabled,
            self.recording_enabled,
            self.comments_enabled,
        )


class StreamerRegistration(StreamerEdit):
    platform: Literal["tiktok", "twitcasting"]
    target: Annotated[str, Field(min_length=1, max_length=2048)]


class ArchiveConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: StrictBool


def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "correlation_id": request.state.correlation_id}},
        status_code=status,
    )


def create_fixture_app(workspace: FixtureWorkspace, port: int = 8083) -> FastAPI:
    if not 1024 <= port <= 65535:
        raise ValueError("invalid fixture port")
    workspace = FixtureWorkspace.open(workspace.root)
    workspace.require_ready()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app.state.csrf_token = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        request.state.correlation_id = str(uuid4())
        request_hosts = request.headers.getlist("host")
        origins = request.headers.getlist("origin")
        changing = request.method not in _SAFE_METHODS
        if len(request_hosts) != 1 or request_hosts[0].lower() not in hosts:
            response = _error(request, 403, "invalid_host", "接続先を確認してください。")
        elif (origins or changing) and origins != [f"http://{request_hosts[0].lower()}"]:
            response = _error(request, 403, "invalid_origin", "同一オリジンから操作してください。")
        else:
            tokens = request.headers.getlist("x-csrf-token")
            token_valid = len(tokens) == 1 and secrets.compare_digest(
                tokens[0].encode("latin-1"), app.state.csrf_token.encode()
            )
            if changing and not token_valid:
                response = _error(request, 403, "invalid_csrf", "操作をもう一度開いてください。")
            else:
                try:
                    response = await call_next(request)
                except (sqlite3.Error, OSError, IsolationError) as error:
                    _LOG.error(
                        "fixture database failure correlation_id=%s exception=%s",
                        request.state.correlation_id,
                        type(error).__name__,
                    )
                    response = _error(request, 503, "workspace_unavailable", "隔離データを読み取れません。")
        response.headers.update(_HEADERS)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exception: RequestValidationError):
        return _error(request, 422, "invalid_input", "入力値を確認してください。")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exception: HTTPException):
        code, message = {
            404: ("not_found", "対象が見つかりません。"),
            405: ("method_not_allowed", "この操作には対応していません。"),
            409: ("account_conflict", "配信者は既に登録されています。"),
            422: ("invalid_input", "入力値を確認してください。"),
        }.get(exception.status_code, ("http_error", "リクエストを処理できません。"))
        return _error(request, exception.status_code, code, message)

    @app.get("/api/v1/health")
    def health():
        with read_database(workspace) as database:
            database.execute("SELECT id FROM stream_sessions LIMIT 1").fetchone()
        return {"mode": "fixture", "alive": True, "ready": True, "recording_available": None}

    @app.get("/api/v1/sessions")
    def sessions(page: Page = 1, page_size: PageSize = 50):
        with read_database(workspace) as database:
            return page_rows(database, session_select, (), "started_at DESC, id DESC", page, page_size)

    @app.get("/api/v1/sessions/{session_id}")
    def session(session_id: SessionId):
        with read_database(workspace) as database:
            found = database.execute(session_select + " WHERE stream_sessions.id = ?", (session_id,)).fetchone()
            if found is None:
                raise HTTPException(404)
            segments = database.execute(
                "SELECT id, number, duration_ms FROM recording_segments WHERE session_id = ? ORDER BY number",
                (session_id,),
            )
            return {**dict(found), "segments": [dict(segment) for segment in segments]}

    @app.get("/api/v1/streamers")
    def streamers(
        platform: Literal["tiktok", "twitcasting"] | None = None,
        page: Page = 1,
        page_size: PageSize = 50,
    ):
        statement = (
            f"SELECT {registry.ACCOUNT_COLUMNS} FROM streamers "
            "WHERE archived_at IS NULL AND (? IS NULL OR platform = ?)"
        )
        with read_database(workspace) as database:
            result = page_rows(database, statement, (platform, platform), "id ASC", page, page_size)
        result["items"] = [registry.account_view(row) for row in result["items"]]
        return result

    @app.post("/api/v1/streamers", status_code=201)
    def add_streamer(input_: StreamerRegistration):
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            try:
                return registry.register(database, input_.platform, input_.target, input_.settings())
            except registry.DuplicateAccount as error:
                raise HTTPException(409) from error
            except ValueError as error:
                raise HTTPException(422) from error

    @app.patch("/api/v1/streamers/{streamer_id}")
    def edit_streamer(streamer_id: SessionId, input_: StreamerEdit):
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            try:
                updated = registry.update(database, streamer_id, input_.settings())
            except ValueError as error:
                raise HTTPException(422) from error
            if updated is None:
                raise HTTPException(404)
            return updated

    @app.delete("/api/v1/streamers/{streamer_id}")
    def archive_streamer(streamer_id: SessionId, confirmation: ArchiveConfirmation):
        if confirmation.confirmed is not True:
            raise HTTPException(422)
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            if not registry.archive(database, streamer_id):
                raise HTTPException(404)
            return {"id": streamer_id, "archived": True}

    @app.get("/api/v1/comments")
    def comments(
        mode: Literal["session", "all"] = "session",
        session_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
        order: Literal["asc", "desc"] = "asc",
        q: Annotated[str, Query(max_length=256)] = "",
        page: Page = 1,
        page_size: PageSize = 50,
    ):
        if mode == "session" and session_id is None:
            return sessions(page, page_size)
        with read_database(workspace) as database:
            return comment_page(database, session_id, q, order, page, page_size)

    mount_pages(app, workspace, port)
    mount_speech_api(app, workspace)

    return app
