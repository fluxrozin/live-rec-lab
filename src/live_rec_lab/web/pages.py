"""Local management views for the selected operations-list layout."""

from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from ..db import connect
from ..isolation import Workspace
from ..retention import managed_recording_capacity, media_deletion_status
from ..runtime import diagnose_fixture
from ..settings import read_operations_settings
from ..speech_jobs import read_speech_configuration
from ..storage_audit import get_storage_audit
from ..streamers import ACCOUNT_COLUMNS, account_view
from ..web_port import PortRevisionConflict, WebPort, read_web_port, save_web_port
from .preferences import DisplayConflict, DisplayPreferences, read_preferences, save_preferences
from .queries import comment_page, history_page, like_pattern, page_rows, read_database, session_select
from .screen_data import (
    ASS_JOB_STATES,
    SPEECH_JOB_STATES,
    active_snapshot,
    disk_summary,
    fixture_events,
    human_size,
    local_time,
    platform_label,
    presentation_status,
    speech_cues,
    speech_queue_page,
    verified_outputs,
)

_NAV = (
    (
        "運用・データ",
        (
            ("dashboard", "/", "ダッシュボード"),
            ("streamers", "/streamers", "配信者一覧"),
            ("recordings", "/recordings", "録画履歴"),
            ("comments", "/comments", "コメント"),
            ("speech", "/speech", "音声字幕"),
        ),
    ),
    ("管理", (("system", "/system", "システム状態"), ("logs", "/logs", "ログ"), ("settings", "/settings", "設定"))),
)
_TITLES = {key: title for _, items in _NAV for key, _, title in items}
_SIZE = 25
Page = Annotated[int, Query(ge=1, le=10_000_000)]
Term = Annotated[str, Query(max_length=256)]
Identity = Annotated[int | None, Query(ge=1, le=2**63 - 1)]
Platform = Literal["", "tiktok", "twitcasting"]


class WebPortUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web_port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    revision: Annotated[int, Field(strict=True, ge=0, le=2**63 - 2)]


def mount_pages(app: FastAPI, workspace: Workspace, port: int) -> None:
    directory = Path(__file__).parent
    templates = Jinja2Templates(directory=directory / "templates")
    templates.env.filters.update(platform=platform_label, local_time=local_time, size=human_size)
    templates.env.globals.update(presentation_status=presentation_status)
    app.mount("/static", StaticFiles(directory=directory / "static"), name="static")

    def render(request: Request, key: str, context: dict, *, view: str | None = None, title: str | None = None):
        with read_database(workspace) as database:
            preferences = read_preferences(database)
            is_recording = bool(
                database.execute("SELECT 1 FROM stream_sessions WHERE recording_state = 'running' LIMIT 1").fetchone()
            )
        page = dict(
            context,
            active_nav=key,
            app_mode=workspace.mode,
            page_title=title or _TITLES[key],
            nav=_NAV,
            preferences=preferences,
            csrf_token=app.state.csrf_token,
            favicon="favicon-recording.svg" if is_recording else "favicon.svg",
            as_of=datetime.now(UTC).isoformat(),
        )
        return templates.TemplateResponse(request=request, name=f"{view or key}.html", context=page)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with read_database(workspace) as database:
            live = active_snapshot(database)
            recent = database.execute(
                session_select
                + " WHERE stream_state != 'live' ORDER BY started_at DESC, stream_sessions.id DESC LIMIT 6"
            ).fetchall()
            preferences = read_preferences(database)
            day = datetime.now(ZoneInfo(preferences.display_timezone)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            bounds = day.astimezone(UTC).isoformat(), (day + timedelta(days=1)).astimezone(UTC).isoformat()
            recent_totals = database.execute(
                """
                SELECT count(*) FILTER (WHERE recording_state = 'needs_review' OR comment_state = 'failed') AS alerts,
                       count(*) FILTER (WHERE recording_state = 'completed' AND started_at >= ? AND started_at < ?)
                         AS today_started_recordings
                FROM stream_sessions
            """,
                bounds,
            ).fetchone()
            today_comments = database.execute(
                """
                SELECT count(*) FROM comments WHERE received_at >= ? AND received_at < ?
                AND NOT EXISTS (
                    SELECT 1 FROM comment_expirations WHERE comment_expirations.session_id = comments.session_id
                )
            """,
                bounds,
            ).fetchone()[0]
        capacity_free, capacity_total = disk_summary(workspace)
        return render(
            request,
            "dashboard",
            {
                **live,
                "recent": recent,
                "capacity_free": capacity_free,
                "capacity_total": capacity_total,
                "alerts": recent_totals["alerts"],
                "today_comments": today_comments,
                "today_recordings": recent_totals["today_started_recordings"],
            },
        )

    @app.get("/dashboard/live", response_class=HTMLResponse)
    def dashboard_live(request: Request):
        with read_database(workspace) as database:
            live = active_snapshot(database)
        return render(request, "dashboard", live, view="dashboard-live")

    @app.get("/streamers", response_class=HTMLResponse)
    def streamer_list(request: Request, q: Term = "", platform: Platform | None = None, page: Page = 1):
        platform = platform or None
        search = like_pattern(q)
        with read_database(workspace) as database:
            result = page_rows(
                database,
                f"""
                SELECT {ACCOUNT_COLUMNS} FROM streamers WHERE archived_at IS NULL AND (? IS NULL OR platform = ?)
                AND (? = '' OR normalized_id LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!')
            """,
                (platform, platform, q, search, search),
                "id ASC",
                page,
                _SIZE,
            )
        result["items"] = [dict(account_view(row), raw_display_name=row["display_name"]) for row in result["items"]]
        return render(request, "streamers", {"result": result, "q": q, "platform": platform})

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings(
        request: Request,
        q: Term = "",
        platform: Platform | None = None,
        streamer_id: Identity = None,
        page: Page = 1,
    ):
        with read_database(workspace) as database:
            result = history_page(database, platform or None, streamer_id, q, page, _SIZE)
            selected = (
                database.execute(
                    "SELECT normalized_id, display_name FROM streamers WHERE id = ?",
                    (streamer_id,),
                ).fetchone()
                if streamer_id
                else None
            )
        return render(
            request,
            "recordings",
            {
                "result": result,
                "q": q,
                "platform": platform,
                "streamer_id": streamer_id,
                "selected": selected,
            },
        )

    @app.get("/recordings/{session_id}", response_class=HTMLResponse)
    def recording_detail(
        request: Request,
        session_id: int,
        back_query: Annotated[str, Query(max_length=2000)] = "",
    ):
        if not 1 <= session_id <= 2**63 - 1:
            raise HTTPException(404)
        with read_database(workspace) as database:
            session = database.execute(session_select + " WHERE stream_sessions.id = ?", (session_id,)).fetchone()
            if session is None:
                raise HTTPException(404)
            outputs = verified_outputs(database, workspace, session_id)
            deletion = media_deletion_status(database, session_id)
            expiry = database.execute(
                "SELECT state, removed_comments FROM comment_expirations WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return render(
            request,
            "recordings",
            {"session": session, "outputs": outputs, "expiry": expiry, "back_query": back_query, "deletion": deletion},
            view="recording-detail",
            title="配信セッション詳細",
        )

    @app.get("/comments", response_class=HTMLResponse)
    def comments(
        request: Request,
        q: Term = "",
        mode: Literal["session", "all"] = "session",
        session_id: Identity = None,
        order: Literal["asc", "desc"] = "asc",
        page: Page = 1,
    ):
        with read_database(workspace) as database:
            session = (
                database.execute(
                    session_select + " WHERE stream_sessions.id = ?",
                    (session_id,),
                ).fetchone()
                if session_id
                else None
            )
            if session_id and session is None:
                raise HTTPException(404)
            expiry = (
                database.execute(
                    "SELECT state FROM comment_expirations WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session_id
                else None
            )
            listing = mode == "session" and session_id is None
            result = (
                history_page(database, None, None, q, page, _SIZE)
                if listing
                else comment_page(
                    database,
                    session_id,
                    q,
                    order,
                    page,
                    _SIZE,
                )
            )
        return render(
            request,
            "comments",
            {
                "result": result,
                "listing": listing,
                "session": session,
                "expiry": expiry,
                "q": q,
                "mode": mode,
                "order": order,
            },
        )

    @app.get("/speech", response_class=HTMLResponse)
    def speech(
        request: Request,
        q: Term = "",
        platform: Platform = "",
        session_id: Identity = None,
        speech_state: Annotated[str, Query(max_length=24)] = "",
        ass_state: Annotated[str, Query(max_length=24)] = "",
        page: Page = 1,
    ):
        if (speech_state and speech_state not in SPEECH_JOB_STATES) or (ass_state and ass_state not in ASS_JOB_STATES):
            raise HTTPException(422)
        with read_database(workspace) as database:
            result = speech_queue_page(
                database,
                workspace,
                platform=platform,
                term=q,
                session_id=session_id,
                speech_state=speech_state,
                ass_state=ass_state,
                page=page,
                page_size=_SIZE,
            )
        return render(
            request,
            "speech",
            {
                "result": result,
                "q": q,
                "platform": platform,
                "session_id": session_id,
                "speech_state": speech_state,
                "ass_state": ass_state,
                "speech_labels": SPEECH_JOB_STATES,
                "ass_labels": ASS_JOB_STATES,
            },
        )

    @app.get("/speech/cues", response_class=HTMLResponse)
    def show_speech(
        request: Request,
        q: Term = "",
        session_id: Identity = None,
        segment_id: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        order: Literal["asc", "desc"] = "asc",
        page: Page = 1,
    ):
        with read_database(workspace) as database:
            if session_id is None and segment_id:
                selected = database.execute(
                    "SELECT session_id FROM recording_segments WHERE id = ?", (segment_id,)
                ).fetchone()
                if selected:
                    session_id = selected["session_id"]
            segments = database.execute(
                "SELECT id, number FROM recording_segments WHERE session_id = ? ORDER BY number", (session_id,)
            ).fetchall()
            items, more, unavailable = speech_cues(
                database, workspace, session_id, q, page, order, segment_id=segment_id or None
            )
        return render(
            request,
            "speech",
            {
                "result": {"items": items, "has_next": more, "page": page},
                "q": q,
                "order": order,
                "session_id": session_id,
                "segment_id": segment_id,
                "segments": segments,
                "unavailable": unavailable,
            },
            view="speech-cues",
            title="発話本文",
        )

    @app.get("/system", response_class=HTMLResponse)
    def system(request: Request):
        measured = diagnose_fixture(workspace)
        capacity_free, capacity_total = disk_summary(workspace)
        with read_database(workspace) as database:
            capacity = managed_recording_capacity(database)
        return render(
            request,
            "system",
            {
                "diagnosis": measured,
                "capacity_free": capacity_free,
                "capacity_total": capacity_total,
                "capacity": capacity,
                "recording_root": workspace.recordings,
                "audit": get_storage_audit(workspace),
                "port": port,
            },
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request, q: Term = "", component: Term = "", level: Term = "", page: Page = 1):
        records, availability = fixture_events(workspace)
        components = sorted({row["component"] for row in records if row["component"]})
        levels = sorted({row["level"] for row in records})
        term = q.casefold()
        filtered = [
            row
            for row in records
            if (not component or row["component"] == component)
            and (not level or row["level"] == level)
            and any(term in value.casefold() for value in row.values())
        ]
        start = (page - 1) * _SIZE
        with read_database(workspace) as database:
            sessions = dict(database.execute("SELECT source_id, id FROM stream_sessions"))
        return render(
            request,
            "logs",
            {
                "availability": availability,
                "components": components,
                "levels": levels,
                "q": q,
                "component": component,
                "level": level,
                "sessions": sessions,
                "result": {
                    "items": filtered[start : start + _SIZE],
                    "total": len(filtered),
                    "page": page,
                    "has_next": start + _SIZE < len(filtered),
                },
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        with read_database(workspace) as database:
            audio = read_speech_configuration(database)
        return render(
            request,
            "settings",
            {
                "audio": audio,
                "web_port": web_port_status(read_web_port(workspace)),
                "operations": read_operations_settings(workspace),
                "recording_root": workspace.recordings,
            },
        )

    def web_port_status(saved: WebPort) -> dict:
        return {**asdict(saved), "current_port": port, "restart_required": saved.web_port != port}

    @app.get("/api/v1/settings/web-port")
    def get_web_port():
        return web_port_status(read_web_port(workspace))

    @app.put("/api/v1/settings/web-port")
    def set_web_port(request: Request, update: WebPortUpdate):
        try:
            return web_port_status(save_web_port(workspace, update.web_port, revision=update.revision))
        except PortRevisionConflict:
            return JSONResponse(
                {
                    "error": {
                        "code": "web_port_conflict",
                        "message": "ポート設定が更新されました。入力内容を控えて再読み込みしてください。",
                        "correlation_id": request.state.correlation_id,
                    }
                },
                status_code=409,
            )

    @app.get("/api/v1/settings/display")
    def get_display_settings():
        with read_database(workspace) as database:
            return read_preferences(database)

    @app.put("/api/v1/settings/display")
    def set_display_settings(request: Request, preferences: DisplayPreferences):
        try:
            with workspace.exclusive(), closing(connect(workspace.database)) as database:
                return save_preferences(database, preferences)
        except DisplayConflict:
            return JSONResponse(
                {
                    "error": {
                        "code": "settings_conflict",
                        "message": "別の画面で設定が更新されました。入力内容を控えて再読み込みしてください。",
                        "correlation_id": request.state.correlation_id,
                    }
                },
                status_code=409,
            )
