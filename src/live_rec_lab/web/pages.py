"""HTML 画面。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from live_rec_lab.comments.srt import generate_segment_srt
from live_rec_lab.config import DEFAULTS, LOG_LEVELS, load_secrets, validate_setting
from live_rec_lab.db.models import (
    CollectionGap,
    Comment,
    CommentCollectionJob,
    DeletionOperation,
    RecordingJob,
    RecordingSegment,
    RuntimeInstance,
    Streamer,
    StreamSession,
    SystemEvent,
)
from live_rec_lab.db.queries import (
    collection_label,
    load_settings,
    recording_summary,
    save_settings,
    search_comments,
    search_sessions,
)
from live_rec_lab.db.session import session_scope
from live_rec_lab.monitoring.state import (
    ACTIVE_COMMENTS,
    ACTIVE_RECORDING,
    COMMENT_LABELS,
    LIVE_LABELS,
    OPEN_SESSION,
    RECORDING_LABELS,
    SESSION_LABELS,
)
from live_rec_lab.ops.log_reader import read_logs
from live_rec_lab.ops.logging_config import redact, set_log_level
from live_rec_lab.recording.engines import media_diagnostics
from live_rec_lab.services.media_files import check_identity, configured_root, managed_path
from live_rec_lab.services.retention import delete_session, deletion_pending
from live_rec_lab.services.sessions import archive_streamer, register_streamer, update_streamer
from live_rec_lab.web.security import tokens_match

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
PLATFORM_NAMES = {"tiktok": "TikTok LIVE", "twitcasting": "ツイキャス"}
NAV = [
    ("運用・データ", [
        ("/", "ダッシュボード"),
        ("/streamers", "配信者一覧"),
        ("/recordings", "録画履歴"),
        ("/comments", "コメント"),
    ]),
    ("管理", [
        ("/system", "システム状態"),
        ("/logs", "ログ"),
        ("/settings", "設定"),
    ]),
]


def _checked(value: str | None) -> bool:
    return value in {"1", "on", "true", "yes"}


def _empty_filter(value: object) -> object:
    return None if value == "" else value


OptionalFilterID = Annotated[Annotated[int, Field(ge=1, le=2**63 - 1)] | None, BeforeValidator(_empty_filter)]


def _fmt(value: datetime | None, tzname: str) -> str:
    if value is None:
        return "未取得"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(tzname)).strftime("%Y-%m-%d %H:%M:%S")


def _day_start(text: str, tzname: str) -> datetime | None:
    if not text:
        return None
    local = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tzname))
    return local.astimezone(UTC)


def _page_url(request: Request, page: int) -> str:
    params = [(key, value) for key, value in request.query_params.multi_items() if key != "page"]
    return request.url.path + "?" + urlencode([*params, ("page", page)])


def _shell(request: Request, db: Session, title: str, asof: str) -> dict:
    settings = load_settings(db)
    path = request.url.path
    return {
        "request": request,
        "csrf": request.state.csrf,
        "font": settings["font"],
        "title": title,
        "asof": asof,
        "nav": NAV,
        "active_path": "/" + path.split("/")[1],
        "settings": settings,
        "recording_active": db.scalar(
            select(select(RecordingJob.id).where(RecordingJob.status == "RECORDING").exists())
        ),
    }


def _render(request: Request, name: str, context: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    with session_scope() as db:
        context = _shell(request, db, "ダッシュボード", "未取得")
        context.update(_metrics(db, context["settings"]))
        return _render(request, "dashboard.html", context)


@router.get("/partials/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request) -> HTMLResponse:
    with session_scope() as db:
        settings = load_settings(db)
        context = {"request": request, **_metrics(db, settings)}
        response = _render(request, "partials/dashboard.html", context)
        response.headers["Cache-Control"] = "no-store"
        return response


def _metrics(db: Session, settings: dict) -> dict:
    now = datetime.now(UTC)
    tzname = str(settings["timezone"])
    start_utc = now.astimezone(ZoneInfo(tzname)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    streamers = select(func.count()).select_from(Streamer).where(Streamer.monitoring_enabled.is_(True))
    monitored = db.scalar(streamers)
    unknown = db.scalar(streamers.where(Streamer.live_status.in_(("UNKNOWN", "ERROR"))))
    sessions = list(db.scalars(
        select(StreamSession).where(StreamSession.status.in_(OPEN_SESSION))
        .order_by(StreamSession.detected_at.desc()).limit(12)
    ))
    recent = list(db.scalars(
        select(StreamSession).where(
            select(RecordingJob.id).where(
                RecordingJob.stream_session_id == StreamSession.id,
                RecordingJob.status.in_(("COMPLETED", "PARTIAL", "CANCELLED", "FAILED")),
            ).exists()
        ).order_by(StreamSession.detected_at.desc()).limit(8)
    ))
    events = db.scalars(
        select(SystemEvent).where(SystemEvent.severity.in_(("warning", "error")))
        .order_by(SystemEvent.created_at.desc()).limit(4)
    )
    root = str(settings["recording_root"])
    available_segments = RecordingSegment.validation_status == "valid", RecordingSegment.files_deleted_at.is_(None)
    count, known_sizes, total_size = db.execute(
        select(func.count(), func.count(RecordingSegment.file_size), func.sum(RecordingSegment.file_size))
        .where(*available_segments)
    ).one()
    free = "未取得"
    capacity_issue = ""
    if root:
        try:
            free_bytes = shutil.disk_usage(root).free
            free = f"{free_bytes / (1024 ** 3):.1f} GiB"
            if free_bytes < settings["min_free_bytes"]:
                capacity_issue = "保存先の空き容量が最低設定値を下回っています。"
        except OSError:
            free = "取得できません"
            capacity_issue = "保存先の空き容量を取得できません。"
    credentials = load_secrets()
    secrets = (credentials.twitcasting_client_id, credentials.twitcasting_client_secret, credentials.sign_api_key)
    return {
        "monitored": monitored,
        "monitor_incomplete": bool(unknown),
        "live_count": db.scalar(streamers.where(Streamer.live_status == "LIVE")),
        "recording_count": db.scalar(
            select(func.count()).select_from(RecordingJob).where(RecordingJob.status == "RECORDING")
        ),
        "collecting_count": db.scalar(
            select(func.count()).select_from(CommentCollectionJob).where(CommentCollectionJob.status == "COLLECTING")
        ),
        "today_count": db.scalar(
            select(func.count()).select_from(RecordingJob).where(RecordingJob.started_at >= start_utc)
        ),
        "today_comments": db.scalar(
            select(func.count()).select_from(Comment).where(Comment.received_at >= start_utc)
        ),
        "total_size": (total_size or 0) if count == known_sizes else None,
        "free_space": free,
        "capacity_issue": capacity_issue,
        "asof": _fmt(now, tzname),
        "sessions": [
            {**_session_row(db, item, tzname), "elapsed": _elapsed(item.platform_started_at, now)}
            for item in sessions
        ],
        "recent": [_session_row(db, item, tzname) for item in recent],
        "events": [_event_summary(item, tzname, secrets) for item in events],
    }


def _elapsed(start: datetime | None, now: datetime) -> str:
    if start is None:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    seconds = int((now - start).total_seconds())
    if start > now:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _session_comments(db: Session, item: StreamSession) -> dict:
    jobs = list(db.scalars(
        select(CommentCollectionJob)
        .where(CommentCollectionJob.stream_session_id == item.id)
        .order_by(CommentCollectionJob.id.desc())
    ))
    latest = jobs[0] if jobs else None
    active = any(job.status in ACTIVE_COMMENTS for job in jobs)
    count = db.scalar(select(func.count(Comment.id)).where(Comment.stream_session_id == item.id))
    purged = item.comments_purged_at is not None
    gaps = db.scalar(select(func.count(CollectionGap.id)).where(
        CollectionGap.stream_session_id == item.id, CollectionGap.job_type == "comments",
    ))
    return {
        "comments": f"期限削除済み（{item.purged_comment_count}件）" if purged else str(count) if count else
        collection_label(latest),
        "comment_status": "保存期限により削除済み" if purged else
        COMMENT_LABELS.get(latest.status, latest.status) if latest else None,
        "comment_error": redact(latest.last_error) if latest and latest.last_error else None,
        "comment_end_reason": latest.end_reason if latest else None,
        "comment_gaps": gaps,
        "comments_updating": active and not purged,
    }


def _session_row(db: Session, item: StreamSession, tzname: str) -> dict:
    streamer = db.get(Streamer, item.streamer_id)
    summary = recording_summary(db, item.id)
    latest = db.scalar(select(RecordingJob).where(RecordingJob.stream_session_id == item.id)
                       .order_by(RecordingJob.id.desc()).limit(1))
    visible = summary["recording_status"]
    status = SESSION_LABELS.get(item.status, item.status)
    if item.status == "LIVE" and streamer and streamer.live_status in {"UNKNOWN", "ERROR", "DISABLED"}:
        status = LIVE_LABELS[streamer.live_status]
    return {
        **summary,
        "id": item.id,
        "who": (streamer.display_name or streamer.platform_user_id) if streamer else "未取得",
        "platform": streamer.platform if streamer else "",
        "platform_name": PLATFORM_NAMES.get(streamer.platform, streamer.platform) if streamer else "未取得",
        "title": item.title or "無題",
        "status": status,
        "last_seen_live": _fmt(item.last_seen_live_at, tzname),
        "recording": (
            "失敗" if summary["latest_recording_status"] == "FAILED" and summary["file_count"] == 0
            else "一部取得・要確認" if visible == "PARTIAL" else RECORDING_LABELS.get(visible, "未録画")
        ),
        "latest_recording": RECORDING_LABELS.get(summary["latest_recording_status"], "未録画"),
        **_session_comments(db, item),
        "when": _fmt(item.detected_at, tzname),
        "started": _fmt(item.platform_started_at, tzname),
        "ended": _fmt(item.platform_ended_at, tzname),
        "converged": _fmt(item.ended_at, tzname),
        "end_reason": item.end_reason,
        "manual_stop": item.manual_stop,
        "open": item.status in OPEN_SESSION,
        "duration": summary["duration_ms"] / 1000 if summary["duration_ms"] is not None else None,
        "recording_error": redact(latest.last_error) if latest and latest.last_error else "",
        "recording_job_id": latest.id if latest else None,
    }


@router.get("/streamers", response_class=HTMLResponse)
def streamers_page(
    request: Request, error: str = "", streamer_id: int | None = Query(None, ge=1, le=2**63 - 1),
    return_logs: str | None = None, platform: str = "",
) -> HTMLResponse:
    with session_scope() as db:
        return _streamers(request, db, error, streamer_id=streamer_id, return_logs=return_logs, platform=platform)


def _streamers(
    request: Request, db: Session, error: str, *, streamer_id: int | None = None, return_logs: str | None = None,
    platform: str = "", submitted: dict | None = None, editor_id: str | int = "new",
) -> HTMLResponse:
    tzname = str(load_settings(db)["timezone"])
    live_status = request.query_params.get("live_status", "")
    rows = []
    stmt = select(Streamer).where(Streamer.archived_at.is_(None)).order_by(Streamer.platform, Streamer.platform_user_id)
    if streamer_id is not None:
        stmt = stmt.where(Streamer.id == streamer_id)
    if platform:
        stmt = stmt.where(Streamer.platform == platform)
    if live_status in LIVE_LABELS:
        stmt = stmt.where(Streamer.live_status == live_status)
    for streamer in db.scalars(stmt):
        recording_job = db.scalar(
            select(RecordingJob).join(StreamSession).where(StreamSession.streamer_id == streamer.id)
            .order_by(RecordingJob.id.desc()).limit(1)
        )
        comment_job = db.scalar(
            select(CommentCollectionJob).join(StreamSession).where(StreamSession.streamer_id == streamer.id)
            .order_by(CommentCollectionJob.id.desc()).limit(1)
        )
        rows.append(
            {
                "id": streamer.id,
                "platform": streamer.platform,
                "account": streamer.platform_user_id,
                "name": streamer.display_name or streamer.platform_user_id,
                "status": LIVE_LABELS.get(streamer.live_status, streamer.live_status),
                "checked": _fmt(streamer.last_checked_at, tzname),
                "last_ok": _fmt(streamer.last_ok_at, tzname),
                "check_failed": streamer.live_status in {"UNKNOWN", "ERROR"},
                "last_live": _fmt(streamer.last_live_at, tzname),
                "last_recording": _fmt(recording_job.started_at, tzname) if recording_job else "未取得",
                "recording_state": RECORDING_LABELS.get(recording_job.status, recording_job.status)
                if recording_job else "未録画",
                "comment_state": COMMENT_LABELS.get(comment_job.status, comment_job.status)
                if comment_job else "未収集",
                "monitoring": streamer.monitoring_enabled,
                "recording": streamer.recording_enabled,
                "comments": streamer.comments_enabled,
            }
        )
    context = _shell(request, db, "配信者一覧", _fmt(datetime.now(UTC), tzname))
    context.update(
        rows=rows, error=error, return_logs=return_logs, filtered=streamer_id is not None,
        saved=request.method == "GET" and request.query_params.get("saved") == "1", platform=platform,
        live_status=live_status, live_labels=LIVE_LABELS, form_query=_streamer_filters(request),
        submitted=submitted, editor_id=editor_id,
    )
    status = 400 if error else 200
    return _render(request, "streamers/list.html", context, status)


def _streamer_filters(request: Request) -> str:
    return urlencode([
        (key, value) for key, value in request.query_params.items()
        if key in {"platform", "live_status", "streamer_id", "return_logs"} and value
    ])


def _streamer_saved(request: Request) -> RedirectResponse:
    query = _streamer_filters(request)
    return RedirectResponse("/streamers?" + (query + "&" if query else "") + "saved=1", status_code=303)


@router.post("/streamers")
def create_streamer(
    request: Request,
    platform: str = Form(),
    account: str = Form(),
    display_name: str = Form(""),
    monitoring_enabled: str | None = Form(None),
    recording_enabled: str | None = Form(None),
    comments_enabled: str | None = Form(None),
    csrf_token: str = Form(),
) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    try:
        if any(value not in {"0", "1"} for value in (monitoring_enabled, recording_enabled, comments_enabled)):
            raise ValueError("監視・録画・コメント収集のON/OFFを指定してください")
        with session_scope(write_lock=True) as db:
            register_streamer(
                db,
                platform=platform,
                account=account,
                display_name=display_name,
                monitoring=_checked(monitoring_enabled),
                recording=_checked(recording_enabled),
                comments=_checked(comments_enabled),
            )
    except ValueError as exc:
        with session_scope() as db:
            return _streamers(
                request, db, str(exc), platform=request.query_params.get("platform", ""),
                submitted={
                    "platform": platform, "account": account, "name": display_name,
                    "monitoring": monitoring_enabled, "recording": recording_enabled, "comments": comments_enabled,
                },
            )
    return _streamer_saved(request)


@router.post("/streamers/{streamer_id}")
async def edit_streamer(
    streamer_id: int,
    request: Request,
    display_name: str = Form(""),
    monitoring_enabled: str | None = Form(None),
    recording_enabled: str | None = Form(None),
    comments_enabled: str | None = Form(None),
    csrf_token: str = Form(),
) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    with session_scope(write_lock=True) as db:
        streamer = db.get(Streamer, streamer_id)
        if streamer is None or streamer.archived_at is not None:
            return _streamers(request, db, "配信者が見つかりません")
        if any(value not in {"0", "1"} for value in (monitoring_enabled, recording_enabled, comments_enabled)):
            return _streamers(
                request, db, "監視・録画・コメント収集のON/OFFを指定してください",
                platform=request.query_params.get("platform", ""), editor_id=streamer_id,
                submitted={
                    "name": display_name, "monitoring": monitoring_enabled,
                    "recording": recording_enabled, "comments": comments_enabled,
                },
            )
        update_streamer(
            db,
            streamer,
            display_name=display_name,
            monitoring=_checked(monitoring_enabled),
            recording=_checked(recording_enabled),
            comments=_checked(comments_enabled),
        )
    await request.app.state.coordinator.enforce_switches(streamer_id)
    return _streamer_saved(request)


@router.post("/streamers/{streamer_id}/delete")
async def remove_streamer(
    streamer_id: int, request: Request, confirm: str = Form(""), csrf_token: str = Form(),
) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    if confirm != "delete":
        with session_scope() as db:
            return _streamers(request, db, "削除の確認がありません")
    with session_scope(write_lock=True) as db:
        streamer = db.get(Streamer, streamer_id)
        if streamer is None:
            return _streamers(request, db, "配信者が見つかりません")
        archived = archive_streamer(db, streamer)
    if archived:
        await request.app.state.coordinator.enforce_switches(streamer_id)
    return _streamer_saved(request)


@router.get("/recordings", response_class=HTMLResponse)
def recordings_page(
    request: Request,
    q: str = "",
    platform: str = "",
    start: str = "",
    end: str = "",
    from_date: str = Query("", alias="from"),
    to_date: str = Query("", alias="to"),
    streamer_id: int | None = Query(None, ge=1, le=2**63 - 1),
    recording_status: str = "",
    sort: str = "",
    page: int = Query(1, ge=1, le=10000),
) -> HTMLResponse:
    start, end = start or from_date, end or to_date
    with session_scope() as db:
        tzname = str(load_settings(db)["timezone"])
        context = _shell(request, db, "録画履歴", "未取得")
        context.update(
            q=q, platform=platform, start=start, end=end, streamer_id=streamer_id,
            recording_status=recording_status, recording_labels=RECORDING_LABELS, sort=sort, page=page,
        )
        try:
            start_dt, end_dt = _date_filters(start, end, tzname)
        except (ValueError, OverflowError):
            context["error"] = "日付範囲を確認してください。"
            return _render(request, "recordings/list.html", context, 400)
        size = 50
        records = search_sessions(
            db, text=q, platform=platform, start=start_dt, end=end_dt,
            streamer_id=streamer_id, recording_status=recording_status, ascending=sort == "oldest",
            offset=(page - 1) * size, limit=size + 1,
        )
        context.update(
            rows=[_session_row(db, item, tzname) for item in records[:size]],
            asof=_fmt(datetime.now(UTC), tzname),
            return_q=request.url.query,
            previous_url=_page_url(request, page - 1) if page > 1 else None,
            next_url=_page_url(request, page + 1) if len(records) > size else None,
            filtered_streamer=db.get(Streamer, streamer_id) if streamer_id else None,
        )
        return _render(request, "recordings/list.html", context)


def _date_filters(start: str, end: str, tzname: str) -> tuple[datetime | None, datetime | None]:
    start_dt = _day_start(start, tzname)
    # Advance the calendar day before converting to UTC. DST days can be 23 or 25 hours.
    end_date = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).date() if end else None
    end_dt = _day_start(end_date.isoformat(), tzname) if end_date else None
    if start_dt and end_dt and start_dt >= end_dt:
        raise ValueError("invalid range")
    return start_dt, end_dt


@router.get("/recordings/{session_id}", response_class=HTMLResponse)
def recording_detail(
    session_id: int, request: Request, return_q: str = "", comment_count_only: bool = False,
    return_logs: str | None = None, return_comments: str | None = None, deleted: str = "",
    srt: str = "", recording_action: str = "",
) -> HTMLResponse:
    with session_scope() as db:
        stream = db.get(StreamSession, session_id)
        if comment_count_only:
            session = {"id": session_id, **(
                _session_comments(db, stream) if stream else {"comments": "未取得", "comment_status": "未取得"}
            )}
            response = _render(request, "recordings/comment_count.html", {
                "session": session,
                "swap_comment_state": request.headers.get("HX-Request") == "true",
            })
            response.headers["Cache-Control"] = "no-store"
            return response
        if stream is None:
            return RedirectResponse("/recordings", status_code=303)
        tzname = str(load_settings(db)["timezone"])
        rows = db.scalars(
            select(RecordingSegment)
            .join(RecordingJob, RecordingSegment.recording_job_id == RecordingJob.id)
            .where(RecordingJob.stream_session_id == session_id)
            .order_by(RecordingSegment.segment_number, RecordingSegment.id)
        )
        latest_operation = db.scalar(select(DeletionOperation).where(
            DeletionOperation.target_id == stream.id,
        ).order_by(DeletionOperation.id.desc()).limit(1))
        pending = deletion_pending(db, stream.id)
        try:
            root = configured_root(db)
        except (OSError, ValueError):
            root = None
        segments = []
        for segment in rows:
            file_status = _file_status(root, segment)
            segments.append({
                "id": segment.id,
                "number": segment.segment_number,
                "status": file_status,
                "validation": segment.validation_status,
                "file_name": Path(segment.file_path).name if segment.file_path else None,
                "file_path": segment.file_path,
                "srt_path": segment.srt_path,
                "srt": {"not_generated": "未生成", "generated": "生成済み", "failed": "生成失敗",
                        "skipped": "字幕対象なし", "pending": "保留", "generating": "生成中",
                        "expired": "保存期限により削除済み", "files_deleted": "ファイル削除済み",
                        "stale": "再同期待ち", "resync_failed": "再同期失敗"}
                .get(segment.srt_status, segment.srt_status),
                "srt_error": redact(segment.srt_error) if segment.srt_error else "",
                "can_regenerate": not pending and not stream.comments_purged_at and file_status == "検証済み",
                "provisional": segment.srt_provisional,
                "offset_ms": segment.srt_manual_offset_ms,
                "job": segment.recording_job_id,
                "start": _fmt(segment.started_at, tzname),
                "end": _fmt(segment.finished_at, tzname),
                "duration": segment.duration_ms / 1000 if segment.duration_ms is not None else "未取得",
                "size": segment.file_size,
                "format": segment.container_format or "未取得",
                "generated": _fmt(segment.srt_generated_at, tzname),
                "sync": {
                    "measured": "実測", "approximate": "近似", "manual": "手動補正", "unsynced": "同期不能",
                }.get(segment.sync_method, "未取得"),
            })
        context = _shell(request, db, "録画履歴", _fmt(datetime.now(UTC), tzname))
        context.update(
            {
                "session": _session_row(db, stream, tzname),
                "segments": segments,
                "media_check_needed": any(
                    segment["validation"] == "valid" and segment["status"] not in {"検証済み", "ファイル削除済み"}
                    for segment in segments
                ),
                "return_q": return_q,
                "return_logs": return_logs,
                "return_comments": return_comments,
                "action_query": urlencode({
                    key: value for key, value in request.query_params.items()
                    if key in {"return_q", "return_logs", "return_comments"}
                }),
                "recording_action": {
                    "stopped": "手動停止を設定しました。録画の確定状況は取得結果で確認できます。",
                    "resumed": "手動停止を解除しました。現在の録画状態は取得結果で確認できます。",
                    "unavailable": "操作できませんでした。現在の配信状態と配信者の監視・録画設定を確認してください。",
                }.get(recording_action),
                "recording_action_failed": recording_action == "unavailable",
                "gaps": [
                    {"type": gap.job_type, "start": _fmt(gap.started_at, tzname),
                     "end": _fmt(gap.ended_at, tzname), "reason": redact(gap.reason)}
                    for gap in db.scalars(
                        select(CollectionGap).where(CollectionGap.stream_session_id == stream.id)
                        .order_by(CollectionGap.started_at)
                    )
                ],
                "error": redact(latest_operation.last_error)
                if latest_operation and latest_operation.last_error else "",
                "deletion": latest_operation,
                "delete_allowed": stream.ended_at is not None and stream.status not in OPEN_SESSION and
                not any(segment["validation"] != "valid" for segment in segments) and
                not any(db.scalar(select(model.id).where(model.stream_session_id == stream.id,
                                                        model.status.in_(states)).limit(1))
                        for model, states in ((RecordingJob, ACTIVE_RECORDING),
                                              (CommentCollectionJob, ACTIVE_COMMENTS))),
                "deleted": deleted == "1",
                "srt_result": {"generated": "字幕を生成しました", "skipped": "字幕対象なし",
                               "failed": "字幕を確定できませんでした", "stale": "再同期待ち"}.get(srt),
                "log_url": "/logs?" + urlencode({"session_id": stream.id}),
            }
        )
        return _render(request, "recordings/detail.html", context)


@router.post("/recordings/{session_id}/stop")
async def stop_session(session_id: int, request: Request, csrf_token: str = Form()) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    stopped = await request.app.state.coordinator.manual_stop(session_id)
    return _session_action_redirect(request, session_id, recording_action="stopped" if stopped else "unavailable")


@router.post("/recordings/{session_id}/resume")
async def resume_session(session_id: int, request: Request, csrf_token: str = Form()) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    resumed = await request.app.state.coordinator.manual_resume(session_id)
    return _session_action_redirect(request, session_id, recording_action="resumed" if resumed else "unavailable")


def _session_action_redirect(request: Request, session_id: int, **result: str) -> RedirectResponse:
    query = {
        key: value for key, value in request.query_params.items()
        if key in {"return_q", "return_logs", "return_comments"}
    }
    encoded = urlencode({**query, **result})
    return RedirectResponse(f"/recordings/{session_id}" + ("?" + encoded if encoded else ""), status_code=303)


@router.post("/recordings/{session_id}/delete")
def remove_session(
    session_id: int, request: Request, confirm: str = Form(""),
    mode: str = Form(""), csrf_token: str = Form(),
) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    if confirm != "delete" or mode not in {"files", "files_and_history"}:
        return HTMLResponse("対象と削除方法を確認してください", status_code=400)
    try:
        result = delete_session(session_id, mode)
    except LookupError:
        return HTMLResponse("対象の配信がありません", status_code=404)
    except ValueError as exc:
        return _operation_error(session_id, str(exc))
    if result.status == "completed":
        if mode == "files_and_history":
            filters = [
                (key, value) for key, value in parse_qsl(request.query_params.get("return_q", ""))
                if key != "deleted"
            ]
            return RedirectResponse("/recordings?" + urlencode([*filters, ("deleted", "history")]), status_code=303)
        return _session_action_redirect(request, session_id, deleted="1")
    return _session_action_redirect(request, session_id)


def _operation_error(session_id: int, message: str) -> HTMLResponse:
    from html import escape

    return HTMLResponse(
        f'<p role="alert">{escape(redact(message))}</p><a href="/recordings/{session_id}">配信詳細に戻る</a>',
        status_code=409,
    )


@router.post("/recordings/segments/{segment_id}/srt")
def regenerate_srt(
    segment_id: int, request: Request, csrf_token: str = Form(), offset_ms: str = Form(""),
) -> HTMLResponse:
    if not tokens_match(request.state.csrf, csrf_token):
        return HTMLResponse("変更を拒否しました", status_code=403)
    with session_scope() as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is None:
            return HTMLResponse("対象セグメントがありません", status_code=404)
        session_id = segment.stream_session_id
        stream = db.get(StreamSession, session_id)
        if stream is None or stream.comments_purged_at or deletion_pending(db, session_id):
            return _operation_error(session_id, "対象の字幕は再生成できません")
    try:
        offset = int(offset_ms) if offset_ms else None
        if offset is not None and abs(offset) > 86_400_000:
            raise ValueError
    except ValueError:
        return _operation_error(session_id, "補正値はミリ秒単位の整数で入力してください")
    result = generate_segment_srt(segment_id, manual_offset_ms=offset)
    return _session_action_redirect(request, session_id, srt=result)


def _file_status(root: Path | None, segment: RecordingSegment) -> str:
    if segment.files_deleted_at is not None:
        return "ファイル削除済み"
    if segment.validation_status != "valid":
        return {"pending": "未確定", "invalid": "検証失敗"}.get(segment.validation_status, "要確認")
    if not root or not segment.file_path:
        return "保存先照合不能"
    try:
        file = managed_path(root, segment.file_path)
        check_identity(file, segment.file_device, segment.file_inode)
        size = file.stat().st_size
        if size == 0:
            return "空ファイル（0 B）"
        return "検証済み" if size == segment.file_size else "サイズ不一致"
    except FileNotFoundError:
        return "ファイル不在"
    except (ValueError, OSError):
        return "ファイル照合不能"


@router.get("/comments", response_class=HTMLResponse)
def comments_page(
    request: Request,
    q: str = "",
    author: str = "",
    author_id: str = "",
    author_name: str = "",
    streamer_id: OptionalFilterID = None,
    platform: str = "",
    session_id: OptionalFilterID = None,
    start: str = "",
    end: str = "",
    from_date: str = Query("", alias="from"),
    to_date: str = Query("", alias="to"),
    page: int = Query(1, ge=1, le=10000),
) -> HTMLResponse:
    start, end = start or from_date, end or to_date
    with session_scope() as db:
        tzname = str(load_settings(db)["timezone"])
        context = _shell(request, db, "コメント", "未取得")
        context.update(
            q=q, author=author, author_id=author_id, author_name=author_name,
            platform=platform, start=start, end=end, session_id=session_id,
            streamer_id=streamer_id, page=page,
        )
        try:
            start_dt, end_dt = _date_filters(start, end, tzname)
        except (ValueError, OverflowError):
            context["error"] = "日付範囲を確認してください。"
            return _render(request, "comments/list.html", context, 400)
        rows, total = search_comments(
            db,
            text=q,
            author=author, author_id=author_id, author_name=author_name,
            streamer_id=streamer_id,
            platform=platform,
            session_id=session_id,
            start=start_dt,
            end=end_dt,
            page=page,
        )
        selected_stream = db.get(StreamSession, session_id) if session_id else None
        context["comments_expired"] = bool(selected_stream and selected_stream.comments_purged_at)
        view = []
        for comment in rows:
            stream = db.get(StreamSession, comment.stream_session_id)
            streamer = db.get(Streamer, stream.streamer_id) if stream else None
            view.append(
                {
                    "at": _fmt(comment.posted_at or comment.received_at, tzname),
                    "time_source": "投稿" if comment.posted_at else "受信",
                    "platform": PLATFORM_NAMES.get(streamer.platform, streamer.platform) if streamer else "未取得",
                    "author_id": comment.platform_user_id,
                    "offset": comment.recording_offset_ms / 1000 if comment.recording_offset_ms is not None else None,
                    "who": comment.nickname or comment.platform_user_id or "不明",
                    "message": comment.message,
                    "streamer": (streamer.display_name or streamer.platform_user_id) if streamer else "未取得",
                    "session_id": comment.stream_session_id,
                    "anonymous": comment.anonymous_label or "",
                }
            )
        context.update(
            rows=view, total=total, asof=_fmt(datetime.now(UTC), tzname),
            return_comments=request.url.query,
            previous_url=_page_url(request, page - 1) if page > 1 else None,
            next_url=_page_url(request, page + 1) if total > page * 50 else None,
        )
        return _render(request, "comments/list.html", context)


@router.get("/system", response_class=HTMLResponse)
def system_page(request: Request) -> HTMLResponse:
    with session_scope() as db:
        settings = load_settings(db)
        tzname = str(settings["timezone"])
        events = db.scalars(
            select(SystemEvent).where(SystemEvent.severity.in_(("warning", "error")))
            .order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc()).limit(10)
        )
        root = str(settings["recording_root"])
        root_state = "未設定"
        free = "未取得"
        if root:
            path = Path(root)
            try:
                free = f"{shutil.disk_usage(path).free / (1024 ** 3):.1f} GiB"
                root_state = _recording_root_status(root)
            except OSError:
                root_state = "取得できません"
        coordinator = request.app.state.coordinator
        gate = coordinator.twitcasting.gate
        api_remaining = gate.remaining
        reset = datetime.fromtimestamp(gate.reset_at, UTC) if not gate.allow("monitor") and gate.reset_at else None
        worker = db.scalar(select(RuntimeInstance).where(RuntimeInstance.instance_id == coordinator.instance_id))
        latest_recovery = db.scalar(
            select(SystemEvent.created_at).where(SystemEvent.event_type == "recording_recovered")
            .order_by(SystemEvent.created_at.desc()).limit(1)
        )
        secrets = load_secrets()
        tools = []
        for name in ("streamlink", "yt-dlp"):
            tools.append({"name": name, "ok": shutil.which(name) is not None})
        pending_deletions = db.scalar(select(func.count(DeletionOperation.id)).where(
            DeletionOperation.status != "completed",
        ))
        pending_segments = db.scalar(select(func.count(RecordingSegment.id)).where(
            RecordingSegment.validation_status == "pending",
        ))
        context = _shell(request, db, "システム状態", _fmt(datetime.now(UTC), tzname))
        context.update(
            {
                "events": [_event_summary(item, tzname, (
                    secrets.twitcasting_client_id, secrets.twitcasting_client_secret, secrets.sign_api_key,
                )) for item in events],
                "checked": _fmt(db.scalar(select(func.max(Streamer.last_checked_at))), tzname),
                "journal": db.connection().exec_driver_sql("PRAGMA journal_mode").scalar_one(),
                "db_path": str(coordinator.paths.db_path),
                "worker_heartbeat": _fmt(worker.last_heartbeat_at if worker else None, tzname),
                "worker_disabled": os.environ.get("LIVE_REC_LAB_DISABLE_SCHEDULER") == "1",
                "retention_ready": coordinator.retention_ready,
                "pending_deletions": pending_deletions,
                "pending_segments": pending_segments,
                "last_recovery": _fmt(latest_recovery, tzname),
                "api_remaining": api_remaining,
                "monitor_retry_at": _fmt(reset, tzname) if reset else "",
                "services": [{
                    "name": label,
                    "last_ok": _fmt(db.scalar(select(func.max(Streamer.last_ok_at)).where(
                        Streamer.platform == platform, Streamer.monitoring_enabled.is_(True),
                    )), tzname),
                    "errors": db.scalar(select(func.count()).select_from(Streamer).where(
                        Streamer.platform == platform, Streamer.monitoring_enabled.is_(True),
                        Streamer.live_status == "ERROR",
                    )),
                } for platform, label in PLATFORM_NAMES.items()],
                "root": root or "未設定",
                "root_state": root_state,
                "free": free,
                "tools": tools,
                "media_tools": media_diagnostics(),
                "twitcasting": (
                    "設定済み" if secrets.twitcasting_client_id and secrets.twitcasting_client_secret else "未設定"
                ),
                "euler": "設定済み" if secrets.sign_api_key else "未設定",
                "versions": _versions(),
                "pid": os.getpid(),
            }
        )
        return _render(request, "system/index.html", context)


def _event_metadata(item: SystemEvent) -> dict:
    try:
        metadata = json.loads(item.metadata_json or "{}")
    except ValueError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _event_summary(item: SystemEvent, tzname: str, secrets: tuple[str, ...]) -> dict:
    time = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=UTC)
    minute = time.replace(second=0, microsecond=0)
    filters = {
        "start": minute.isoformat(timespec="minutes"),
        "end": (minute + timedelta(minutes=1)).isoformat(timespec="minutes"),
        "level": item.severity.upper(),
    }
    metadata = _event_metadata(item)
    for key in ("component", "correlation_id"):
        value = metadata.get(key)
        if isinstance(value, str) and 0 < len(value) <= 256 and redact(value, secrets) == value:
            filters[key] = value
    if item.stream_session_id:
        filters["session_id"] = item.stream_session_id
    streamer_id = metadata.get("streamer_id")
    if type(streamer_id) is int and 0 < streamer_id <= 2**63 - 1:
        filters["streamer_id"] = streamer_id
    return {
        "at": _fmt(item.created_at, tzname),
        "message": redact(item.message, secrets),
        "severity": item.severity,
        "session_id": item.stream_session_id,
        "log_url": "/logs?" + urlencode(filters),
    }


def _parse_log_time(value: str, tzname: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tzname))
    return parsed.astimezone(UTC)


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    q: str = Query("", max_length=500),
    level: str = Query("", max_length=32),
    component: str = Query("", max_length=256),
    start: str = Query("", max_length=40),
    end: str = Query("", max_length=40),
    correlation_id: str = Query("", max_length=256),
    session_id: OptionalFilterID = None,
    streamer_id: OptionalFilterID = None,
    page: int = Query(1, ge=1, le=10000),
) -> HTMLResponse:
    with session_scope() as db:
        context = _shell(request, db, "ログ", "未取得")
    tzname = str(context["settings"]["timezone"])
    filters = {
        "q": q, "level": level, "component": component, "start": start, "end": end,
        "correlation_id": correlation_id, "session_id": session_id, "streamer_id": streamer_id,
    }
    context.update(filters)
    try:
        start_time = _parse_log_time(start, tzname)
        end_time = _parse_log_time(end, tzname)
        for field, value in (("start", start_time), ("end", end_time)):
            if value is not None:
                context[field] = value.astimezone(ZoneInfo(tzname)).strftime("%Y-%m-%dT%H:%M")
        if start_time and end_time and start_time >= end_time:
            raise ValueError("invalid range")
    except (ValueError, OverflowError):
        context["error"] = "開始・終了の日時と範囲を確認してください。"
        return _render(request, "logs/index.html", context, 400)

    page_size = 50
    credentials = load_secrets()
    secrets = (credentials.twitcasting_client_id, credentials.twitcasting_client_secret, credentials.sign_api_key)
    result = read_logs(
        request.app.state.coordinator.paths.log_dir,
        start=start_time, end=end_time, level=level, component=component, query=q,
        correlation_id=correlation_id, streamer_id=streamer_id, session_id=session_id,
        page=page, page_size=page_size, secrets=secrets,
    )
    with session_scope() as db:
        session_ids = {item.stream_session_id for item in result.entries if item.stream_session_id is not None}
        streamer_ids = {item.streamer_id for item in result.entries if item.streamer_id is not None}
        sessions = set(db.scalars(select(StreamSession.id).where(StreamSession.id.in_(session_ids))))
        streamers = set(db.scalars(select(Streamer.id).where(
            Streamer.id.in_(streamer_ids), Streamer.archived_at.is_(None),
        )))
        summaries = []
        if page == 1 and (session_id or correlation_id or streamer_id):
            events = select(SystemEvent).order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
            if session_id:
                events = events.where(SystemEvent.stream_session_id == session_id)
            if start_time:
                events = events.where(SystemEvent.created_at >= start_time)
            if end_time:
                events = events.where(SystemEvent.created_at <= end_time)
            if level:
                events = events.where(func.upper(SystemEvent.severity) == level.upper())
            for item in db.scalars(events.limit(100)):
                metadata = _event_metadata(item)
                if correlation_id and metadata.get("correlation_id") != correlation_id:
                    continue
                if component and metadata.get("component") != component:
                    continue
                if streamer_id and metadata.get("streamer_id") != streamer_id:
                    continue
                summary = _event_summary(item, tzname, secrets)
                if not q or q.casefold() in summary["message"].casefold():
                    summaries.append(summary)
                if len(summaries) == 10:
                    break
    query_values = {key: value for key, value in filters.items() if value not in (None, "")}
    context.update(
        result=result, page=page, filtered=bool(query_values), event_summaries=summaries,
        oldest=_fmt(result.oldest, tzname), newest=_fmt(result.newest, tzname),
        asof="取得失敗" if result.unavailable else _fmt(datetime.now(UTC), tzname),
        previous_url="/logs?" + urlencode({**query_values, "page": page - 1}) if page > 1 else None,
        next_url="/logs?" + urlencode({**query_values, "page": page + 1}) if result.total > page * page_size else None,
        rows=[{
            "entry": item, "at": _fmt(item.timestamp, tzname),
            "key": hashlib.sha256(json.dumps(asdict(item), sort_keys=True, default=str).encode()).hexdigest(),
            "summary": item.message.split("\n", 1)[0][:160],
            "session_link": item.stream_session_id if item.stream_session_id in sessions else None,
            "streamer_link": item.streamer_id if item.streamer_id in streamers else None,
        } for item in result.entries],
    )
    response = _render(request, "logs/index.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


def _versions() -> list[dict[str, str]]:
    found = []
    for package in ("tiktoklive", "streamlink", "yt-dlp", "fastapi"):
        try:
            found.append({"name": package, "version": version(package)})
        except Exception:
            found.append({"name": package, "version": "未取得"})
    return found


_SETTINGS_SECTIONS = {
    "monitor": ("監視", ("monitor_per_minute_tiktok", "monitor_per_minute_twitcasting")),
    "recording": (
        "録画・保存",
        ("max_parallel_recordings", "recording_root", "min_free_bytes", "max_recording_bytes", "retention_days",
         "recording_quality", "tiktok_engine_first", "auto_delete_recordings"),
    ),
    "comments": ("コメント・SRT", ("comments_enabled", "srt_display_seconds", "comment_retention_days")),
    "display": ("表示", ("timezone", "font")),
    "advanced": ("詳細設定", ("log_level", "web_port")),
}


def _settings_revision(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def _recording_root_status(root: str) -> str:
    from live_rec_lab.storage import recording_root_status

    if not root:
        return "未設定"
    try:
        recording_root_status(Path(root))
    except (OSError, ValueError) as error:
        return redact(str(error))
    return "既存の録画先との分離とドライブを確認しました。"


def _render_settings(
    request: Request,
    *,
    section: str,
    saved: bool = False,
    submitted: dict | None = None,
    errors: dict | None = None,
    error: str = "",
    status: int = 200,
) -> HTMLResponse:
    with session_scope() as db:
        context = _shell(request, db, "設定", _fmt(datetime.now(UTC), str(load_settings(db)["timezone"])))
    settings = context["settings"]
    coordinator = request.app.state.coordinator
    secrets = load_secrets()
    gate = coordinator.twitcasting.gate
    context.update(
        form={**settings, "port_mode": "fixed" if settings["web_port"] else "auto", **(submitted or {})},
        revision=_settings_revision(settings),
        dirty=submitted is not None,
        section=section if section in _SETTINGS_SECTIONS else "monitor",
        sections=_SETTINGS_SECTIONS,
        errors=errors or {},
        error=error,
        conflict=status == 409,
        saved=saved,
        log_levels=[(level.lower(), level) for level in LOG_LEVELS],
        restart_needed=settings["web_port"] != coordinator.startup_web_port,
        bound_port=os.environ.get("LIVE_REC_LAB_BOUND_PORT"),
        active_port_mode="固定" if coordinator.startup_web_port else "自動割り当て",
        gate_remaining=gate.remaining,
        monitor_paused=not gate.allow("monitor"),
        recording_root_status=_recording_root_status(str(settings["recording_root"])),
        twitcasting_configured=bool(secrets.twitcasting_client_id and secrets.twitcasting_client_secret),
        sign_api_configured=bool(secrets.sign_api_key),
    )
    return _render(request, "settings/index.html", context, status)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, section: str = "monitor", saved: bool = False) -> HTMLResponse:
    return _render_settings(request, section=section, saved=saved)


async def _read_settings_form(request: Request) -> FormData:
    return await request.form()


@router.post("/settings", response_class=HTMLResponse)
def save_settings_page(request: Request, form: FormData = Depends(_read_settings_form)):
    if not tokens_match(request.state.csrf, str(form.get("csrf_token", ""))):
        return HTMLResponse("変更を拒否しました", status_code=403)
    section = str(form.get("section", "monitor"))
    section = section if section in _SETTINGS_SECTIONS else "monitor"
    submitted = {}
    for key in DEFAULTS:
        field = "timezone_name" if key == "timezone" else key
        if field in form:
            submitted[key] = str(form[field])
    payload = dict(submitted)
    port_mode = str(form.get("port_mode", ""))
    if port_mode:
        submitted["port_mode"] = port_mode
    if port_mode == "auto":
        payload["web_port"] = "0"
    errors = {}
    for key, value in payload.items():
        try:
            payload[key] = validate_setting(key, value)
        except (TypeError, ValueError, OverflowError):
            errors[key] = "入力値と指定範囲を確認してください。"
    if port_mode == "fixed" and not payload.get("web_port"):
        errors["web_port"] = "固定時は 1〜65535 のポート番号を入力してください。"
    if errors:
        section = next(name for name, (_, fields) in _SETTINGS_SECTIONS.items() if errors.keys() & set(fields))
        return _render_settings(
            request, section=section, submitted=submitted, errors=errors,
            error="保存できませんでした。入力内容を確認してください。", status=400,
        )
    try:
        with session_scope() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            revision = str(form.get("revision", ""))
            conflict = bool(revision and revision != _settings_revision(load_settings(db)))
            if not conflict:
                settings = save_settings(db, payload)
    except SQLAlchemyError:
        return _render_settings(
            request, section=section, submitted=submitted,
            error="データベースに保存できませんでした。再度お試しください。", status=503,
        )
    if conflict:
        return _render_settings(
            request, section=section, submitted=submitted,
            error="別の操作で設定が変更されました。保存済みの値と編集内容を確認してください。", status=409,
        )
    set_log_level(str(settings["log_level"]))
    return RedirectResponse(f"/settings?section={section}&saved=1", status_code=303)
