"""ローカルの管理用 JSON API。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from live_rec_lab.comments.srt import generate_segment_srt
from live_rec_lab.comments.store import comment_writes_blocked
from live_rec_lab.config import load_secrets
from live_rec_lab.db.models import CollectionGap, RecordingSegment, Streamer, StreamSession, SystemEvent
from live_rec_lab.db.queries import (
    load_settings,
    recording_summary,
    save_settings,
    search_comments,
    search_sessions,
    settings_revision,
)
from live_rec_lab.db.session import session_scope
from live_rec_lab.ops.logging_config import redact, set_log_level
from live_rec_lab.services.media_files import configured_root
from live_rec_lab.services.retention import delete_session
from live_rec_lab.services.sessions import archive_streamer, register_streamer, update_streamer
from live_rec_lab.web.security import tokens_match

router = APIRouter(prefix="/api/v1")


class StreamerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["tiktok", "twitcasting"]
    account: str = Field(min_length=1, max_length=300)
    display_name: str = Field(default="", max_length=128)
    monitoring_enabled: StrictBool
    recording_enabled: StrictBool
    comments_enabled: StrictBool


class StreamerEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=128)
    monitoring_enabled: StrictBool | None = None
    recording_enabled: StrictBool | None = None
    comments_enabled: StrictBool | None = None


class SrtRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_ms: int | None = Field(default=None, strict=True, ge=-86_400_000, le=86_400_000)


def _csrf(request: Request) -> None:
    if not tokens_match(getattr(request.state, "csrf", None), request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail="変更を拒否しました")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _streamer(streamer: Streamer) -> dict:
    return {
        "id": streamer.id,
        "platform": streamer.platform,
        "platform_user_id": streamer.platform_user_id,
        "display_name": streamer.display_name,
        "monitoring_enabled": streamer.monitoring_enabled,
        "recording_enabled": streamer.recording_enabled,
        "comments_enabled": streamer.comments_enabled,
        "live_status": streamer.live_status,
        "last_checked_at": _utc(streamer.last_checked_at),
        "last_ok_at": _utc(streamer.last_ok_at),
        "last_live_at": _utc(streamer.last_live_at),
    }


@router.get("/health")
def health(request: Request) -> dict:
    coordinator = request.app.state.coordinator
    return {"status": "ok", "instance_id": coordinator.instance_id, "pid": os.getpid()}


@router.get("/streamers")
def list_streamers() -> dict:
    with session_scope() as db:
        rows = db.scalars(select(Streamer).where(Streamer.archived_at.is_(None)).order_by(Streamer.id))
        return {"items": [_streamer(item) for item in rows]}


@router.post("/streamers", status_code=201, dependencies=[Depends(_csrf)])
def create_streamer(request: Request, body: StreamerCreate) -> dict:
    try:
        with session_scope(write_lock=True) as db:
            streamer = register_streamer(
                db, platform=body.platform, account=body.account, display_name=body.display_name,
                monitoring=body.monitoring_enabled, recording=body.recording_enabled, comments=body.comments_enabled,
            )
            return _streamer(streamer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/streamers/{streamer_id}", dependencies=[Depends(_csrf)])
async def patch_streamer(streamer_id: int, request: Request, body: StreamerEdit) -> dict:
    with session_scope(write_lock=True) as db:
        streamer = db.get(Streamer, streamer_id)
        if streamer is None or streamer.archived_at is not None:
            raise HTTPException(status_code=404, detail="配信者が見つかりません")
        updates = body.model_dump(exclude_none=True)
        update_streamer(
            db, streamer, display_name=updates.get("display_name", streamer.display_name or ""),
            monitoring=updates.get("monitoring_enabled", streamer.monitoring_enabled),
            recording=updates.get("recording_enabled", streamer.recording_enabled),
            comments=updates.get("comments_enabled", streamer.comments_enabled),
        )
        payload = _streamer(streamer)
    await request.app.state.coordinator.enforce_switches(streamer_id)
    return payload


@router.delete("/streamers/{streamer_id}", dependencies=[Depends(_csrf)])
async def remove_streamer(streamer_id: int, request: Request, confirm: Literal["delete"]) -> dict:
    with session_scope(write_lock=True) as db:
        streamer = db.get(Streamer, streamer_id)
        if streamer is None:
            raise HTTPException(status_code=404, detail="配信者が見つかりません")
        archived = archive_streamer(db, streamer)
    if archived:
        await request.app.state.coordinator.enforce_switches(streamer_id)
    return {"id": streamer_id, "archived": archived}


@router.get("/sessions")
def list_sessions(
    q: str = "", platform: str = "", streamer_id: int | None = Query(None, ge=1, le=2**63 - 1),
    recording_status: str = "", start: datetime | None = None, end: datetime | None = None,
    page: int = Query(1, ge=1, le=1_000_000),
) -> dict:
    with session_scope() as db:
        rows = search_sessions(
            db, text=q, platform=platform, streamer_id=streamer_id, recording_status=recording_status,
            start=_utc(start), end=_utc(end), offset=(page - 1) * 50, limit=51,
        )
        return {"items": [_session(db, item) for item in rows[:50]], "page": page, "has_next": len(rows) > 50}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, include_segments: bool = True) -> dict:
    with session_scope() as db:
        stream = db.get(StreamSession, session_id)
        if stream is None:
            raise HTTPException(status_code=404, detail="配信記録がありません")
        payload = _session(db, stream)
        gaps = db.scalars(select(CollectionGap).where(CollectionGap.stream_session_id == stream.id)
                          .order_by(CollectionGap.started_at, CollectionGap.id))
        payload["gaps"] = [
            {"kind": item.job_type, "started_at": _utc(item.started_at), "ended_at": _utc(item.ended_at),
             "reason": redact(item.reason)}
            for item in gaps
        ]
        if include_segments:
            segments = db.scalars(select(RecordingSegment).where(
                RecordingSegment.stream_session_id == stream.id,
            ).order_by(RecordingSegment.segment_number, RecordingSegment.id))
            payload["segments"] = [
                {"id": item.id, "segment_number": item.segment_number, "validation_status": item.validation_status,
                 "duration_ms": item.duration_ms, "file_size": item.file_size, "srt_status": item.srt_status,
                 "srt_provisional": item.srt_provisional, "files_deleted_at": _utc(item.files_deleted_at)}
                for item in segments
            ]
        return payload


@router.post("/sessions/{session_id}/stop", dependencies=[Depends(_csrf)])
async def stop_session(session_id: int, request: Request) -> dict:
    try:
        result = await request.app.state.coordinator.manual_stop(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is False:
        raise HTTPException(status_code=409, detail="停止できる稼働中の配信がありません")
    return {"session_id": session_id, "manual_stop_requested": True}


@router.post("/sessions/{session_id}/resume", dependencies=[Depends(_csrf)])
async def resume_session(session_id: int, request: Request) -> dict:
    try:
        result = await request.app.state.coordinator.manual_resume(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is False:
        raise HTTPException(status_code=409, detail="配信状態と録画設定を確認してください")
    return {"session_id": session_id, "manual_resume_requested": True}


@router.delete("/sessions/{session_id}", dependencies=[Depends(_csrf)])
def remove_session(
    session_id: int, request: Request, confirm: Literal["delete"], mode: Literal["files", "files_and_history"],
) -> dict:
    try:
        result = delete_session(session_id, mode)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = asdict(result)
    if result.status != "completed":
        raise HTTPException(status_code=409, detail=payload)
    return payload


@router.get("/comments")
def list_comments(
    q: str = "", author: str = "", author_id: str = "", author_name: str = "", platform: str = "",
    streamer_id: int | None = Query(None, ge=1, le=2**63 - 1),
    session_id: int | None = Query(None, ge=1, le=2**63 - 1),
    start: datetime | None = None, end: datetime | None = None, page: int = Query(1, ge=1, le=1_000_000),
) -> dict:
    with session_scope() as db:
        rows, total = search_comments(
            db, text=q, author=author, author_id=author_id, author_name=author_name,
            platform=platform, streamer_id=streamer_id, session_id=session_id,
            start=_utc(start), end=_utc(end), page=page,
        )
        stream = db.get(StreamSession, session_id) if session_id else None
        return {
            "items": [
                {"id": item.id, "message": item.message, "nickname": item.nickname,
                 "platform_user_id": item.platform_user_id, "posted_at": _utc(item.posted_at),
                 "received_at": _utc(item.received_at), "stream_session_id": item.stream_session_id,
                 "anonymous_label": item.anonymous_label, "recording_offset_ms": item.recording_offset_ms}
                for item in rows
            ],
            "page": page,
            "page_size": 50,
            "total": total,
            "comments_purged_at": _utc(stream.comments_purged_at) if stream else None,
            "purged_comment_count": stream.purged_comment_count if stream else None,
        }


@router.get("/settings")
def get_settings() -> dict:
    with session_scope() as db:
        values = load_settings(db)
    secrets = load_secrets()
    return {
        **values,
        "revision": settings_revision(values),
        "twitcasting_configured": bool(secrets.twitcasting_client_id and secrets.twitcasting_client_secret),
        "euler_configured": bool(secrets.sign_api_key),
    }


@router.put("/settings", dependencies=[Depends(_csrf)])
def put_settings(request: Request, body: dict = Body()) -> dict:
    updates = dict(body)
    revision = updates.pop("revision", None)
    if not revision:
        raise HTTPException(status_code=422, detail="設定の取得版を指定してください")
    try:
        with session_scope(write_lock=True) as db:
            if revision != settings_revision(load_settings(db)):
                raise HTTPException(status_code=409, detail="他の操作で設定が変更されました")
            values = save_settings(db, updates)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=redact(str(exc))) from exc
    set_log_level(str(values["log_level"]))
    return {**values, "revision": settings_revision(values)}


@router.get("/system")
def system() -> dict:
    with session_scope() as db:
        settings = load_settings(db)
        try:
            configured_root(db)
            reason = None
        except (ValueError, OSError) as exc:
            reason = redact(str(exc))
        events = list(db.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(20)))
        return {
            "recording_root": settings["recording_root"],
            "recording_root_ready": reason is None,
            "recording_root_reason": reason,
            "events": [
                {"message": redact(item.message), "severity": item.severity,
                 "correlation_id": redact(item.correlation_id) if item.correlation_id else None}
                for item in events
            ],
        }


@router.post("/segments/{segment_id}/srt", dependencies=[Depends(_csrf)])
async def rebuild_srt(segment_id: int, request: Request, body: SrtRequest | None = None) -> dict:
    with session_scope() as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is None:
            raise HTTPException(status_code=404, detail="セグメントがありません")
        stream = db.get(StreamSession, segment.stream_session_id)
        if stream is None or comment_writes_blocked(db, stream) or segment.files_deleted_at:
            raise HTTPException(status_code=409, detail="対象の字幕は再生成できません")
    offset = body.offset_ms if body else None
    status = await asyncio.to_thread(generate_segment_srt, segment_id, manual_offset_ms=offset)
    with session_scope() as db:
        segment = db.get(RecordingSegment, segment_id)
        error = redact(segment.srt_error) if segment and segment.srt_error else None
        return {"srt_status": status, "srt_error": error, "srt_provisional": bool(segment and segment.srt_provisional)}


def _session(db: Session, stream: StreamSession) -> dict:
    streamer = db.get(Streamer, stream.streamer_id)
    return {
        "id": stream.id,
        "platform_stream_id": stream.platform_stream_id,
        "title": stream.title,
        "status": stream.status,
        "manual_stop": stream.manual_stop,
        "ended_at": _utc(stream.ended_at),
        "platform_ended_at": _utc(stream.platform_ended_at),
        "end_reason": stream.end_reason,
        "comments_purged_at": _utc(stream.comments_purged_at),
        "purged_comment_count": stream.purged_comment_count,
        "streamer": _streamer(streamer) if streamer else None,
        **recording_summary(db, stream.id),
    }
