"""読取モデル（画面・APIが共通で使う短い読取取引）。ページングは total/page/page_size/has_next/as_ofを返す。"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, or_, select

from live_rec_lab.db.models import (
    collection_gaps,
    comment_collection_jobs,
    comments,
    recording_jobs,
    recording_segments,
    stream_sessions,
    streamers,
    system_events,
)
from live_rec_lab.runtime import utcnow

REC_ACTIVE = ("STARTING", "RECORDING", "RECONNECTING", "FINALIZING")
CMT_ACTIVE = ("CONNECTING", "COLLECTING", "RECONNECTING")
MAX_PAGE_SIZE = 200


class PageError(ValueError):
    pass


def page_args(page: int, size: int) -> tuple[int, int]:
    if page < 1 or not (1 <= size <= MAX_PAGE_SIZE):
        raise PageError("page は1以上、page_size は1〜200で指定してください")
    return page, size


def paged(rows: list, total: int, page: int, size: int) -> dict:
    return {"items": rows, "total": total, "page": page, "page_size": size, "has_next": page * size < total,
            "as_of": utcnow()}


def like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------- sessions
def _session_rows(conn: Connection, where, page: int, size: int) -> tuple[list[dict], int]:
    base = select(stream_sessions, streamers.c.display_name, streamers.c.platform, streamers.c.platform_user_id,
                  streamers.c.folder_name).join(streamers, streamers.c.id == stream_sessions.c.streamer_id)
    for w in where:
        base = base.where(w)
    total = conn.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    q = base.order_by(stream_sessions.c.detected_at.desc(), stream_sessions.c.id.desc()).limit(size).offset((page - 1) * size)
    rows = [dict(r) for r in conn.execute(q).mappings()]
    for r in rows:
        r.update(_session_stats(conn, r["id"]))
    return rows, total


def _session_stats(conn: Connection, sid: int) -> dict:
    rec = conn.execute(select(func.count()).where(recording_jobs.c.stream_session_id == sid,
                                                  recording_jobs.c.status.in_(REC_ACTIVE))).scalar()
    cmt = conn.execute(select(func.count()).where(comment_collection_jobs.c.stream_session_id == sid,
                                                  comment_collection_jobs.c.status.in_(CMT_ACTIVE))).scalar()
    seg = conn.execute(select(func.count(), func.coalesce(func.sum(recording_segments.c.file_size), 0),
                              func.coalesce(func.sum(recording_segments.c.duration_ms), 0)).where(
        recording_segments.c.stream_session_id == sid, recording_segments.c.validation_status == "valid")).one()
    bad = conn.execute(select(func.count()).where(recording_segments.c.stream_session_id == sid,
                                                  recording_segments.c.validation_status == "invalid")).scalar()
    ncom = conn.execute(select(func.count()).where(comments.c.stream_session_id == sid)).scalar()
    gaps = conn.execute(select(func.count()).where(collection_gaps.c.stream_session_id == sid)).scalar()
    return {"recording": bool(rec), "collecting": bool(cmt), "segments": seg[0], "size": seg[1], "duration_ms": seg[2],
            "invalid_segments": bad, "comment_count": ncom, "gap_count": gaps}


def list_sessions(conn: Connection, page: int = 1, size: int = 50, *, q: str = "", platform: str = "", status: str = "") -> dict:
    page, size = page_args(page, size)
    where = []
    if q:
        where.append(or_(streamers.c.display_name.like(like(q), escape="\\"), streamers.c.platform_user_id.like(like(q), escape="\\"),
                         stream_sessions.c.title.like(like(q), escape="\\")))
    if platform in ("tiktok", "twitcasting"):
        where.append(streamers.c.platform == platform)
    if status in ("LIVE", "ENDING", "ENDED"):
        where.append(stream_sessions.c.status == status)
    rows, total = _session_rows(conn, where, page, size)
    return paged(rows, total, page, size)


def active_sessions(conn: Connection) -> list[dict]:
    rows, _ = _session_rows(conn, [stream_sessions.c.status.in_(("LIVE", "ENDING"))], 1, 50)
    return rows


def session_detail(conn: Connection, sid: int) -> dict | None:
    rows, _ = _session_rows(conn, [stream_sessions.c.id == sid], 1, 1)
    if not rows:
        return None
    s = rows[0]
    s["segment_list"] = [dict(r) for r in conn.execute(select(recording_segments).where(
        recording_segments.c.stream_session_id == sid).order_by(recording_segments.c.segment_number, recording_segments.c.id)).mappings()]
    s["recording_jobs"] = [dict(r) for r in conn.execute(select(recording_jobs).where(
        recording_jobs.c.stream_session_id == sid).order_by(recording_jobs.c.id)).mappings()]
    s["comment_jobs"] = [dict(r) for r in conn.execute(select(comment_collection_jobs).where(
        comment_collection_jobs.c.stream_session_id == sid).order_by(comment_collection_jobs.c.id)).mappings()]
    s["gaps"] = [dict(r) for r in conn.execute(select(collection_gaps).where(
        collection_gaps.c.stream_session_id == sid).order_by(collection_gaps.c.id)).mappings()]
    s["events"] = [dict(r) for r in conn.execute(select(system_events).where(
        system_events.c.stream_session_id == sid).order_by(system_events.c.id.desc()).limit(30)).mappings()]
    return s


# ---------------------------------------------------------------- dashboard
def dashboard(conn: Connection, recording_root: Path, tz: str, period: str = "24h") -> dict:
    now = datetime.now(UTC)
    tzinfo = ZoneInfo(tz)
    today0 = datetime.now(tzinfo).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    since = now - {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}.get(period, timedelta(hours=24))
    st_total = conn.execute(select(func.count()).where(streamers.c.archived_at.is_(None))).scalar()
    st_mon = conn.execute(select(func.count()).where(streamers.c.archived_at.is_(None), streamers.c.monitoring_enabled.is_(True))).scalar()
    act = active_sessions(conn)
    rec_n = sum(1 for s in act if s["recording"])
    cmt_n = sum(1 for s in act if s["collecting"])

    def iso(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    today_sessions = conn.execute(select(func.count()).where(stream_sessions.c.detected_at >= iso(today0))).scalar()
    today_comments = conn.execute(select(func.count()).where(comments.c.effective_at >= iso(today0))).scalar()
    total_size = conn.execute(select(func.coalesce(func.sum(recording_segments.c.file_size), 0)).where(
        recording_segments.c.validation_status == "valid")).scalar()
    warnings = [dict(r) for r in conn.execute(select(system_events).where(
        system_events.c.severity.in_(("warning", "error")), system_events.c.created_at >= iso(since)
    ).order_by(system_events.c.id.desc()).limit(6)).mappings()]
    warn_n = conn.execute(select(func.count()).where(system_events.c.severity.in_(("warning", "error")),
                                                     system_events.c.created_at >= iso(since))).scalar()
    free = total = None
    try:
        if recording_root.exists():
            u = shutil.disk_usage(recording_root)
            free, total = u.free, u.total
    except OSError:
        pass
    recent, _ = _session_rows(conn, [stream_sessions.c.status == "ENDED"], 1, 5)
    return {"streamers_total": st_total, "streamers_monitored": st_mon, "live": len(act), "recording": rec_n,
            "collecting": cmt_n, "today_sessions": today_sessions, "today_comments": today_comments,
            "managed_bytes": total_size, "disk_free": free, "disk_total": total, "active": act,
            "recent": recent, "warnings": warnings, "warning_count": warn_n, "period": period, "as_of": utcnow()}


# ---------------------------------------------------------------- comments
def search_comments(conn: Connection, *, mode: str = "all", session_id: int | None = None, q: str = "", user: str = "",
                    platform: str = "", streamer_id: int | None = None, date_from: datetime | None = None,
                    date_to: datetime | None = None, order: str = "desc", page: int = 1, size: int = 50) -> dict:
    page, size = page_args(page, size)
    if order not in ("asc", "desc"):
        raise PageError("order は asc/desc のみ指定できます")
    base = select(comments, stream_sessions.c.title.label("session_title"), streamers.c.display_name.label("streamer_name"),
                  streamers.c.platform).join(stream_sessions, stream_sessions.c.id == comments.c.stream_session_id
                                             ).join(streamers, streamers.c.id == stream_sessions.c.streamer_id)
    if mode == "session" and session_id:
        base = base.where(comments.c.stream_session_id == session_id)
    if q:
        base = base.where(comments.c.message.like(like(q), escape="\\"))
    if user:
        base = base.where(or_(comments.c.nickname.like(like(user), escape="\\"),
                              comments.c.platform_user_id.like(like(user), escape="\\")))
    if platform in ("tiktok", "twitcasting"):
        base = base.where(streamers.c.platform == platform)
    if streamer_id:
        base = base.where(stream_sessions.c.streamer_id == streamer_id)
    if date_from:
        base = base.where(comments.c.effective_at >= date_from.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    if date_to:
        base = base.where(comments.c.effective_at < date_to.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    total = conn.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    ordering = (comments.c.effective_at.asc(), comments.c.id.asc()) if order == "asc" else (
        comments.c.effective_at.desc(), comments.c.id.desc())
    rows = [dict(r) for r in conn.execute(base.order_by(*ordering).limit(size).offset((page - 1) * size)).mappings()]
    return paged(rows, total, page, size)


def sessions_with_comments(conn: Connection, page: int = 1, size: int = 50, q: str = "") -> dict:
    """配信別閲覧の一覧（コメント件数つき）。"""
    page, size = page_args(page, size)
    cnt = select(comments.c.stream_session_id.label("sid"), func.count().label("n")).group_by(comments.c.stream_session_id).subquery()
    base = select(stream_sessions.c.id, stream_sessions.c.title, stream_sessions.c.detected_at, stream_sessions.c.status,
                  streamers.c.display_name, streamers.c.platform, cnt.c.n).join(
        streamers, streamers.c.id == stream_sessions.c.streamer_id).join(cnt, cnt.c.sid == stream_sessions.c.id)
    if q:
        base = base.where(or_(streamers.c.display_name.like(like(q), escape="\\"), stream_sessions.c.title.like(like(q), escape="\\")))
    total = conn.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = [dict(r) for r in conn.execute(base.order_by(stream_sessions.c.detected_at.desc(), stream_sessions.c.id.desc()
                                                        ).limit(size).offset((page - 1) * size)).mappings()]
    return paged(rows, total, page, size)


# ---------------------------------------------------------------- logs (構造化ログファイルの安全な読取)
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def read_logs(log_dir: Path, *, level: str = "", q: str = "", source: str = "", page: int = 1, size: int = 50,
              max_lines: int = 20000) -> dict:
    page, size = page_args(page, size)
    files = [log_dir / "app.log"] + [log_dir / f"app.log.{i}" for i in range(1, 5)]
    lines: list[str] = []
    for f in files:  # 新しい順: 現行→世代
        if not f.is_file():
            continue
        try:
            lines.extend(reversed(f.read_text("utf-8", "replace").splitlines()))
        except OSError:
            continue
        if len(lines) >= max_lines:
            break
    entries = []
    for ln in lines[:max_lines]:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if level and LEVELS.get(e.get("level", ""), 0) < LEVELS.get(level, 0):
            continue
        if source and source not in e.get("source", ""):
            continue
        if q and q not in e.get("message", ""):
            continue
        entries.append(e)
    total = len(entries)
    return paged(entries[(page - 1) * size: page * size], total, page, size) | {"truncated": len(lines) > max_lines}


def recent_events(conn: Connection, page: int = 1, size: int = 50, severity: str = "") -> dict:
    page, size = page_args(page, size)
    base = select(system_events)
    if severity:
        base = base.where(system_events.c.severity == severity)
    total = conn.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = [dict(r) for r in conn.execute(base.order_by(system_events.c.id.desc()).limit(size).offset((page - 1) * size)).mappings()]
    return paged(rows, total, page, size)


def segment_list(conn: Connection, page: int = 1, size: int = 50) -> dict:
    page, size = page_args(page, size)
    base = select(recording_segments, streamers.c.display_name, stream_sessions.c.detected_at).join(
        stream_sessions, stream_sessions.c.id == recording_segments.c.stream_session_id).join(
        streamers, streamers.c.id == stream_sessions.c.streamer_id).where(recording_segments.c.validation_status == "valid")
    total = conn.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = [dict(r) for r in conn.execute(base.order_by(stream_sessions.c.detected_at.desc(), recording_segments.c.id.desc()
                                                        ).limit(size).offset((page - 1) * size)).mappings()]
    return paged(rows, total, page, size)
