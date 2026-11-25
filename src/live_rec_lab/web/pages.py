"""画面用HTMLルーター（8主要画面 + セッション詳細 + 部分更新）。GETは参照のみ。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from live_rec_lab import queries, settings_store, storage, streamers
from live_rec_lab.web.ui import render

router = APIRouter()
CATS = [("monitor", "監視"), ("recording", "録画・保存"), ("comments", "コメント・SRT"), ("speech", "音声字幕"),
        ("display", "表示"), ("advanced", "詳細設定")]


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _base_url(request: Request, path: str) -> str:
    q = {k: v for k, v in request.query_params.items() if k != "page" and v != ""}
    return path + "?" + (urlencode(q) + "&" if q else "")


def _day(s: str | None, tz: str, end: bool = False) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    from datetime import timedelta

    d = d.replace(tzinfo=ZoneInfo(tz))
    return d + timedelta(days=1) if end else d


def _add_recording_sizes(rows: list[dict], pending: list[dict]) -> None:
    """確定容量に書き込み中の実サイズを足す。ファイルの確認はDB取引の外で行う。"""
    by_session = {s["id"]: s for s in rows}
    for seg in pending:
        session = by_session[seg["stream_session_id"]]
        if not session["recording_root"]:
            continue
        root = Path(session["recording_root"])
        path = Path(seg["temp_path"])
        if not root.is_absolute() or not path.is_relative_to(root) or not path.name.endswith(".raw" + storage.TMP_SUFFIX):
            continue
        try:
            directory = path.parent.resolve(strict=True)
            if not directory.is_relative_to(root.resolve(strict=True)):
                continue
            info = (directory / path.name).lstat()
        except (OSError, ValueError, RuntimeError):
            # 書き込み開始前や確定時の入れ替えで一時ファイルが見えないことがある。
            continue
        if S_ISREG(info.st_mode):
            session["size"] += info.st_size
            for row in session.get("segment_list", ()):
                if row["id"] == seg["id"]:
                    row["file_size"] = info.st_size
                    break


def _dashboard_data(request: Request) -> dict:
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        st = settings_store.get_all(conn)
        d = queries.dashboard(conn, Path(st["recording_root"] or ctx.env.recording_root), st["display_timezone"],
                              request.query_params.get("period", "24h"))
        pending = queries.pending_recordings(conn, [s["id"] for s in d["active"] if s["recording"]])
    _add_recording_sizes(d["active"], pending)
    return d


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", "dashboard", d=_dashboard_data(request))


@router.get("/partials/active-sessions", response_class=HTMLResponse)
def active_partial(request: Request):
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        active = queries.active_sessions(conn)
        pending = queries.pending_recordings(conn, [s["id"] for s in active if s["recording"]])
    _add_recording_sizes(active, pending)
    return render(request, "partials/active_sessions.html", "dashboard", d={"active": active})


@router.get("/streamers", response_class=HTMLResponse)
def streamers_page(request: Request):
    ctx = request.app.state.ctx
    q, platform = request.query_params.get("q", "").strip().lower(), request.query_params.get("platform", "")
    sup = ctx.services.get("supervisor")
    with ctx.engine.begin() as conn:
        allrows = streamers.list_active(conn)
        active = {s["streamer_id"]: s for s in queries.active_sessions(conn)}
    rows = []
    for s in allrows:
        if q and q not in s["display_name"].lower() and q not in s["platform_user_id"]:
            continue
        if platform and s["platform"] != platform:
            continue
        s["active_session"] = active.get(s["id"])
        s["diag"] = sup.diag.get(s["id"]) if sup else None
        rows.append(s)
    return render(request, "streamers.html", "streamers", rows=rows, total=len(allrows), q=q, platform=platform)


@router.get("/recordings", response_class=HTMLResponse)
def recordings(request: Request):
    ctx = request.app.state.ctx
    qp = request.query_params
    try:
        with ctx.engine.begin() as conn:
            res = queries.list_sessions(conn, _int(qp.get("page"), 1), _int(qp.get("page_size"), 50), q=qp.get("q", ""),
                                        platform=qp.get("platform", ""), status=qp.get("status", ""))
            pending = queries.pending_recordings(conn, [s["id"] for s in res["items"] if s["recording"]])
    except queries.PageError as exc:
        return HTMLResponse(str(exc), status_code=422)
    _add_recording_sizes(res["items"], pending)
    return render(request, "recordings.html", "recordings", res=res, q=qp.get("q", ""), platform=qp.get("platform", ""),
                  status=qp.get("status", ""), base_url=_base_url(request, "/recordings"))


@router.get("/recordings/{session_id}", response_class=HTMLResponse)
def recording_detail(request: Request, session_id: int):
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        s = queries.session_detail(conn, session_id)
        pending = queries.pending_recordings(conn, [session_id]) if s and s["recording"] else []
    if s is None:
        return render(request, "recordings.html", "recordings", status_code=404, res=queries.paged([], 0, 1, 50), q="",
                      platform="", status="", base_url="/recordings?")
    _add_recording_sizes([s], pending)
    return render(request, "recording_detail.html", "recordings", s=s)


@router.get("/comments", response_class=HTMLResponse)
def comments_page(request: Request):
    ctx = request.app.state.ctx
    qp = request.query_params
    tz = ctx.services.get("tz") or "Asia/Tokyo"
    with ctx.engine.begin() as conn:
        tz = settings_store.get(conn, "display_timezone")
    mode = qp.get("mode", "all")
    session_id = _int(qp.get("session_id"), 0) or None
    order = qp.get("order", "asc" if mode == "session" else "desc")
    page, size = _int(qp.get("page"), 1), _int(qp.get("page_size"), 50)
    res = sess = None
    error = None
    try:
        with ctx.engine.begin() as conn:
            if mode == "session" and not session_id:
                sess = queries.sessions_with_comments(conn, page, size, qp.get("q", ""))
            else:
                res = queries.search_comments(
                    conn, mode=mode, session_id=session_id, q=qp.get("q", "").strip(), user=qp.get("user", "").strip(),
                    platform=qp.get("platform", ""), date_from=_day(qp.get("date_from"), tz),
                    date_to=_day(qp.get("date_to"), tz, True), order=order, page=page, size=size)
    except queries.PageError as exc:
        error = str(exc)
        res = queries.paged([], 0, 1, 50)
    return render(request, "comments.html", "comments", res=res or queries.paged([], 0, 1, 50), sess=sess, mode=mode,
                  session_id=session_id, q=qp.get("q", ""), user=qp.get("user", ""), platform=qp.get("platform", ""),
                  date_from=qp.get("date_from", ""), date_to=qp.get("date_to", ""), order=order, error=error,
                  base_url=_base_url(request, "/comments"))


@router.get("/speech", response_class=HTMLResponse)
def speech_page(request: Request):
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        res = queries.segment_list(conn, _int(request.query_params.get("page"), 1), 50)
    return render(request, "speech.html", "speech", res=res, base_url=_base_url(request, "/speech"))


@router.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        root = Path(settings_store.get(conn, "recording_root") or ctx.env.recording_root)
        events = queries.recent_events(conn, 1, 8)["items"]
    sup = ctx.services.get("supervisor")
    return render(request, "system.html", "system", tools=ctx.tools, root=root, storage=storage.check_root(root),
                  events=events, scan=sup.scan_state if sup else None)


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    ctx = request.app.state.ctx
    qp = request.query_params
    try:
        res = queries.read_logs(ctx.env.log_dir, level=qp.get("level", ""), q=qp.get("q", ""), source=qp.get("source", ""),
                                page=_int(qp.get("page"), 1), size=_int(qp.get("page_size"), 50))
    except queries.PageError as exc:
        return HTMLResponse(str(exc), status_code=422)
    return render(request, "logs.html", "logs", res=res, level=qp.get("level", ""), q=qp.get("q", ""),
                  source=qp.get("source", ""), base_url=_base_url(request, "/logs"))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        s = settings_store.get_all(conn)
    return render(request, "settings.html", "settings", s=s, cats=CATS, running_port=ctx.port)


@router.get("/speech/cues", response_class=HTMLResponse)
def speech_cues_page(request: Request):
    from live_rec_lab.web import api

    qp = request.query_params
    try:
        data = api.speech_cues(request)
        import json as _json

        res = _json.loads(data.body)
    except api.ApiError as exc:
        return HTMLResponse(exc.message, status_code=exc.status)
    return render(request, "speech_cues.html", "speech", res=res, q=qp.get("q", ""), order=qp.get("order", "asc"),
                  base_url=_base_url(request, "/speech/cues"))
