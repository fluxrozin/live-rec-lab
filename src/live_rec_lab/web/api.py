"""JSON REST API（/api/v1）。UI・CLIと同じ業務サービスを呼ぶ。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from live_rec_lab import archives, queries, settings_store, streamers
from live_rec_lab.db.models import recording_segments, stream_sessions
from live_rec_lab.runtime import utcnow
from live_rec_lab.settings_defs import DEFINITIONS, INTERNAL_KEYS

router = APIRouter(prefix="/api/v1")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict | None = None, extra: dict | None = None):
        self.status, self.code, self.message, self.fields, self.extra = status, code, message, fields, extra or {}


def error_response(exc: ApiError) -> JSONResponse:
    body = {"error": {"code": exc.code, "message": exc.message, "correlation_id": uuid.uuid4().hex[:12], **exc.extra}}
    if exc.fields:
        body["error"]["fields"] = exc.fields
    return JSONResponse(body, status_code=exc.status)


def _no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"重複キー: {k}")
        d[k] = v
    return d


async def read_json(request: Request) -> dict:
    if "application/json" not in request.headers.get("content-type", ""):
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type は application/json を指定してください")
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise ApiError(413, "BODY_TOO_LARGE", "本文が大きすぎます")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup,
                          parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "BAD_JSON", f"JSONを解析できません: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(422, "VALIDATION_ERROR", "オブジェクトで指定してください")
    return data


def _ctx(request: Request):
    return request.app.state.ctx


def _sup(request: Request):
    return _ctx(request).services.get("supervisor")


def _page_params(request: Request) -> tuple[int, int]:
    try:
        return int(request.query_params.get("page", 1)), int(request.query_params.get("page_size", 50))
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "page / page_size は整数で指定してください") from exc


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(data, default=str, ensure_ascii=False)), status_code=status)


# ------------------------------------------------------------------ health / system / dashboard
@router.get("/health")
def health(request: Request):
    ctx = _ctx(request)
    ready = ctx.tools.ok
    body = {"alive": True, "ready": ready, "instance_id": ctx.instance_id, "mode": ctx.env.mode,
            "ffmpeg": ctx.tools.ffmpeg_version, "media_tools_ok": ready, "scheduler": not ctx.scheduler_disabled}
    return JSONResponse(body, status_code=200 if ready else 503)


@router.get("/system")
def system(request: Request):
    from live_rec_lab.diagnose import collect

    return _json(collect(_ctx(request).env))


@router.get("/dashboard")
def dashboard(request: Request):
    ctx = _ctx(request)
    period = request.query_params.get("period", "24h")
    if period not in ("24h", "7d", "30d"):
        raise ApiError(422, "VALIDATION_ERROR", "period は 24h / 7d / 30d")
    with ctx.engine.begin() as conn:
        st = settings_store.get_all(conn)
        return _json(queries.dashboard(conn, Path(st["recording_root"] or ctx.env.recording_root),
                                       st["display_timezone"], period))


# ------------------------------------------------------------------ streamers
@router.get("/streamers")
def list_streamers(request: Request):
    with _ctx(request).engine.begin() as conn:
        rows = streamers.list_active(conn)
    q = request.query_params.get("q", "").lower()
    platform = request.query_params.get("platform", "")
    rows = [r for r in rows if (not q or q in r["display_name"].lower() or q in r["platform_user_id"])
            and (not platform or r["platform"] == platform)]
    return _json({"items": rows, "total": len(rows)})


@router.post("/streamers")
async def create_streamer(request: Request):
    data = await read_json(request)
    unknown = set(data) - {"platform", "account", "display_name", "monitoring_enabled", "recording_enabled",
                           "comments_enabled"}
    if unknown:
        raise ApiError(422, "VALIDATION_ERROR", "未知のキーです", {k: "未知のキー" for k in unknown})
    try:
        with _ctx(request).engine.begin() as conn:
            row = streamers.register(conn, streamers.StreamerInput(
                data.get("platform", ""), data.get("account", ""), data.get("display_name"),
                data.get("monitoring_enabled"), data.get("recording_enabled"), data.get("comments_enabled")))
    except streamers.InputError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "入力が不正です", exc.fields) from exc
    except streamers.Conflict as exc:
        raise ApiError(409, "CONFLICT", str(exc)) from exc
    return _json(row, 201)


@router.patch("/streamers/{streamer_id}")
async def patch_streamer(request: Request, streamer_id: int):
    data = await read_json(request)
    try:
        with _ctx(request).engine.begin() as conn:
            row = streamers.update_settings(conn, streamer_id, **data)
    except streamers.InputError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "入力が不正です", exc.fields) from exc
    except streamers.NotFound as exc:
        raise ApiError(404, "NOT_FOUND", "配信者が見つかりません") from exc
    sup = _sup(request)
    if sup:
        await sup.streamer_changed(streamer_id)
    return _json(row)


@router.delete("/streamers/{streamer_id}")
async def delete_streamer(request: Request, streamer_id: int):
    if request.query_params.get("confirm") != "true":
        raise ApiError(422, "VALIDATION_ERROR", "削除には confirm=true が必要です")
    sup = _sup(request)
    if sup and sup.has_active_session(streamer_id):
        raise ApiError(409, "CONFLICT", "稼働中のセッションがあるため削除できません。監視をOFFにして停止してください")
    try:
        with _ctx(request).engine.begin() as conn:
            streamers.archive(conn, streamer_id)
    except streamers.NotFound as exc:
        raise ApiError(404, "NOT_FOUND", "配信者が見つかりません") from exc
    return _json({"deleted": True})


@router.get("/streamers/order")
def get_order(request: Request):
    with _ctx(request).engine.begin() as conn:
        rows = streamers.list_active(conn)
        rev = settings_store.get(conn, "streamer_order_revision")
        mode = settings_store.get(conn, "streamer_order_mode")
    return _json({"mode": mode, "ordered_ids": [r["id"] for r in rows], "order_revision": rev})


@router.put("/streamers/order")
async def put_order(request: Request):
    from live_rec_lab.db.models import streamers as st

    data = await read_json(request)
    ids, rev = data.get("ordered_ids"), data.get("order_revision")
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ApiError(422, "VALIDATION_ERROR", "ordered_ids は整数の配列で指定してください")
    with _ctx(request).engine.begin() as conn:
        cur = {r["id"] for r in streamers.list_active(conn)}
        if set(ids) != cur or len(ids) != len(cur):
            raise ApiError(422, "VALIDATION_ERROR", "ordered_ids は有効な全配信者を重複なく含める必要があります")
        current = settings_store.get(conn, "streamer_order_revision")
        if rev != current:
            raise ApiError(409, "CONFLICT", "並び順が他で更新されています", extra={"current_revision": current})
        for pos, sid in enumerate(ids, 1):
            conn.execute(update(st).where(st.c.id == sid).values(sort_order=-pos))  # 一意制約の衝突回避
        for pos, sid in enumerate(ids, 1):
            conn.execute(update(st).where(st.c.id == sid).values(sort_order=pos, updated_at=utcnow()))
        settings_store.update(conn, {"streamer_order_revision": current + 1, "streamer_order_mode": "custom"}, None)
    return _json({"ordered_ids": ids, "order_revision": current + 1})


# ------------------------------------------------------------------ sessions
@router.get("/sessions")
def list_sessions(request: Request):
    page, size = _page_params(request)
    qp = request.query_params
    try:
        with _ctx(request).engine.begin() as conn:
            return _json(queries.list_sessions(conn, page, size, q=qp.get("q", ""), platform=qp.get("platform", ""),
                                               status=qp.get("status", "")))
    except queries.PageError as exc:
        raise ApiError(422, "VALIDATION_ERROR", str(exc)) from exc


@router.get("/sessions/{session_id}")
def get_session(request: Request, session_id: int):
    with _ctx(request).engine.begin() as conn:
        s = queries.session_detail(conn, session_id)
    if s is None:
        raise ApiError(404, "NOT_FOUND", "セッションが見つかりません")
    return _json(s)


@router.post("/sessions/{session_id}/stop")
async def stop_session(request: Request, session_id: int):
    sup = _sup(request)
    if not sup or not await sup.stop_recording(session_id):
        raise ApiError(409, "CONFLICT", "録画中のセッションではありません")
    return _json({"accepted": True}, 202)


# ------------------------------------------------------------------ comments
@router.get("/comments")
def comments(request: Request):
    page, size = _page_params(request)
    qp = request.query_params
    ctx = _ctx(request)
    try:
        with ctx.engine.begin() as conn:
            tz = settings_store.get(conn, "display_timezone")

            def day(s, end=False):
                if not s:
                    return None
                try:
                    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz))
                except ValueError as exc:
                    raise ApiError(422, "VALIDATION_ERROR", "日付は YYYY-MM-DD で指定してください") from exc
                from datetime import timedelta

                return d + timedelta(days=1) if end else d

            res = queries.search_comments(
                conn, mode=qp.get("mode", "all"), session_id=int(qp["session_id"]) if qp.get("session_id") else None,
                q=qp.get("q", ""), user=qp.get("user", ""), platform=qp.get("platform", ""),
                date_from=day(qp.get("date_from")), date_to=day(qp.get("date_to"), True), order=qp.get("order", "desc"),
                page=page, size=size)
    except (queries.PageError, ValueError) as exc:
        raise ApiError(422, "VALIDATION_ERROR", str(exc)) from exc
    return _json(res)


# ------------------------------------------------------------------ settings
@router.get("/settings")
def get_settings(request: Request):
    ctx = _ctx(request)
    with ctx.engine.begin() as conn:
        s = settings_store.public_settings(conn)
    return _json({"settings": s, "version": s["settings_version"], "running_port": ctx.port})


@router.put("/settings")
async def put_settings(request: Request):
    data = await read_json(request)
    values, version = data.get("settings"), data.get("version")
    if not isinstance(values, dict) or not isinstance(version, int) or isinstance(version, bool):
        raise ApiError(422, "VALIDATION_ERROR", "settings(オブジェクト)と version(整数)が必要です")
    values = {k: v for k, v in values.items() if k != "settings_version"}
    if any(k in INTERNAL_KEYS or k not in DEFINITIONS for k in values):
        raise ApiError(422, "VALIDATION_ERROR", "未知の設定キーです",
                       {k: "未知の設定キーです" for k in values if k in INTERNAL_KEYS or k not in DEFINITIONS})
    try:
        with _ctx(request).engine.begin() as conn:
            if "recording_root" in values:
                _check_recording_root(request, values["recording_root"])
            new = settings_store.update(conn, values, version)
            out = settings_store.public_settings(conn)
    except settings_store.SettingErrors as exc:
        raise ApiError(422, "VALIDATION_ERROR", "設定値が不正です", exc.fields) from exc
    except settings_store.VersionConflict as exc:
        raise ApiError(409, "CONFLICT", "設定が他で更新されています", extra={"current_version": exc.current}) from exc
    if "log_level" in values:  # 保存直後に反映
        import logging

        logging.getLogger("live_rec_lab").setLevel(values["log_level"])
    sup = _sup(request)
    if sup and "comments_globally_enabled" in values:  # 全体コメントON/OFFを稼働中のセッションへ反映
        for run in list(sup.runs.values()):
            await sup.reconcile(run)
    return _json({"settings": out, "version": new})


def _check_recording_root(request: Request, value) -> None:
    """保存先の変更は境界検証を通ったものだけ受理（保護パス・管理領域との重複・隔離の許可範囲）。"""
    from live_rec_lab.config import ConfigError, Environment, validate_boundaries

    ctx = _ctx(request)
    try:
        from live_rec_lab.config import norm

        env = Environment(ctx.env.dev, ctx.env.data_dir, ctx.env.state_dir, norm(value), ctx.env.default_port,
                          ctx.env.secrets, ctx.env.flags)
        validate_boundaries(env)
    except ConfigError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "保存先を変更できません", {"recording_root": str(exc)}) from exc


# ------------------------------------------------------------------ segments
@router.post("/segments/{segment_id}/srt")
async def regenerate_srt(request: Request, segment_id: int):
    data = {}
    if request.headers.get("content-length", "0") != "0":
        data = await read_json(request)
    offset = data.get("offset_ms")
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
        raise ApiError(422, "VALIDATION_ERROR", "offset_ms は整数(ms)で指定してください")
    ctx = _ctx(request)
    with ctx.engine.begin() as conn:
        seg = conn.execute(select(recording_segments).where(recording_segments.c.id == segment_id)).mappings().first()
        if seg is None:
            raise ApiError(404, "NOT_FOUND", "セグメントが見つかりません")
        sess = conn.execute(select(stream_sessions).where(stream_sessions.c.id == seg["stream_session_id"])).mappings().one()
    if sess["status"] != "ENDED":
        raise ApiError(409, "CONFLICT", "収集が収束するまで字幕を生成できません")
    sup = _sup(request)
    if sup is None:
        raise ApiError(503, "UNAVAILABLE", "監督が起動していません")
    result = await asyncio.to_thread(sup.generate_segment_srt, segment_id, offset)
    return _json(result, 200 if result["status"] != "failed" else 409)


# ------------------------------------------------------------------ speech / ASS
def _segment_ctx(request: Request, segment_id: int):
    ctx = _ctx(request)
    with ctx.engine.begin() as conn:
        seg = conn.execute(select(recording_segments).where(recording_segments.c.id == segment_id)).mappings().first()
        if seg is None:
            raise ApiError(404, "NOT_FOUND", "セグメントが見つかりません")
        sess = conn.execute(select(stream_sessions).where(stream_sessions.c.id == seg["stream_session_id"])).mappings().one()
    return dict(seg), dict(sess)


@router.get("/speech")
def speech_list(request: Request):
    page, size = _page_params(request)
    try:
        with _ctx(request).engine.begin() as conn:
            return _json(queries.segment_list(conn, page, size))
    except queries.PageError as exc:
        raise ApiError(422, "VALIDATION_ERROR", str(exc)) from exc


@router.get("/speech/cues")
def speech_cues(request: Request):
    from live_rec_lab import speech as speech_mod

    page, size = _page_params(request)
    qp = request.query_params
    order = qp.get("order", "asc")
    if order not in ("asc", "desc"):
        raise ApiError(422, "VALIDATION_ERROR", "order は asc/desc")
    ctx = _ctx(request)
    with ctx.engine.begin() as conn:
        q = select(recording_segments, stream_sessions.c.recording_root).join(
            stream_sessions, stream_sessions.c.id == recording_segments.c.stream_session_id).where(
            recording_segments.c.speech_status == "generated")
        if qp.get("segment_id"):
            q = q.where(recording_segments.c.id == int(qp["segment_id"]))
        if qp.get("session_id"):
            q = q.where(recording_segments.c.stream_session_id == int(qp["session_id"]))
        segs = [dict(r) for r in conn.execute(q.order_by(recording_segments.c.stream_session_id, recording_segments.c.segment_number)).mappings()]
    needle = qp.get("q", "")
    rows = []
    for seg in segs:  # 管理済み音声SRTを安全に解析（ファイル本体は返さない）
        f = Path(seg["recording_root"]) / seg["speech_srt_path"]
        try:
            cues = speech_mod.parse_srt(f.read_text("utf-8"))
        except OSError:
            continue
        for a, b, t in cues:
            if not needle or needle in t:
                rows.append({"segment_id": seg["id"], "session_id": seg["stream_session_id"], "start_ms": a, "end_ms": b, "text": t})
    rows.sort(key=lambda r: (r["session_id"], r["segment_id"], r["start_ms"]), reverse=(order == "desc"))
    total = len(rows)
    return _json(queries.paged(rows[(page - 1) * size: page * size], total, page, size))


@router.post("/segments/{segment_id}/speech")
async def regen_speech(request: Request, segment_id: int):
    seg, sess = _segment_ctx(request, segment_id)
    if sess["status"] != "ENDED":
        raise ApiError(409, "CONFLICT", "収集が収束するまで生成できません")
    if seg["validation_status"] != "valid":
        raise ApiError(409, "CONFLICT", "確定した動画がありません")
    if seg["speech_status"] == "running":
        raise ApiError(409, "CONFLICT", "処理中です")
    sup = _sup(request)
    sup.speech.enqueue_segment(segment_id)
    return _json({"accepted": True, "segment_id": segment_id}, 202)


@router.post("/segments/{segment_id}/ass")
async def regen_ass(request: Request, segment_id: int):
    seg, sess = _segment_ctx(request, segment_id)
    if sess["status"] != "ENDED" or seg["validation_status"] != "valid":
        raise ApiError(409, "CONFLICT", "収束済みの確定動画のみ対象です")
    sup = _sup(request)
    res = await asyncio.to_thread(sup.speech.generate_ass, segment_id, None, None)
    return _json(res, 200 if res["status"] != "failed" else 409)


@router.post("/speech/retry-failed")
async def retry_failed(request: Request):
    ctx = _ctx(request)
    with ctx.engine.begin() as conn:
        ids = [r[0] for r in conn.execute(select(recording_segments.c.id).where(
            recording_segments.c.speech_status.in_(("failed", "blocked")), recording_segments.c.validation_status == "valid"))]
    sup = _sup(request)
    for i in ids:
        sup.speech.enqueue_segment(i)
    return _json({"accepted": len(ids), "rejected": 0}, 202)


# ------------------------------------------------------------------ 削除・手動収束・照合
@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: int):
    if request.query_params.get("confirm") != "true":
        raise ApiError(422, "VALIDATION_ERROR", "アーカイブ全体の削除には confirm=true が必要です")
    ctx = _ctx(request)
    sup = _sup(request)
    if sup and session_id in sup.runs:
        raise ApiError(409, "CONFLICT", "稼働中・処理中のアーカイブは削除できません")
    try:
        res = await asyncio.to_thread(archives.delete_archive, ctx.engine, ctx.env, session_id, "manual")
    except LookupError as exc:
        raise ApiError(404, "NOT_FOUND", "セッションが見つかりません") from exc
    except archives.Refused as exc:
        raise ApiError(409, "CONFLICT", str(exc)) from exc
    return _json(res, 200 if res["status"] == "completed" else 409 if res["status"] == "partial" else 200)


@router.post("/sessions/{session_id}/manual-convergence")
async def manual_convergence(request: Request, session_id: int):
    data = await read_json(request)
    if data.get("confirm") is not True:
        raise ApiError(422, "VALIDATION_ERROR", "confirm: true が必要です")
    sup = _sup(request)
    try:
        await asyncio.to_thread(archives.manual_converge, _ctx(request).engine, session_id, str(data.get("reason", "")),
                                bool(sup and session_id in sup.runs))
    except LookupError as exc:
        raise ApiError(404, "NOT_FOUND", "セッションが見つかりません") from exc
    except archives.Refused as exc:
        raise ApiError(409, "CONFLICT", str(exc)) from exc
    return _json({"manual_convergence": True})


@router.post("/system/scans")
async def start_scan(request: Request):
    sup = _sup(request)
    if sup is None:
        raise ApiError(503, "UNAVAILABLE", "監督が起動していません")
    return _json(sup.start_scan(), 202)


@router.get("/system/scans/latest")
def latest_scan(request: Request):
    sup = _sup(request)
    st = sup.scan_state if sup else {"id": None, "running": False, "result": None}
    return _json({"scan_id": st["id"], "running": st["running"], "result": st["result"]})


@router.get("/system/scans/{scan_id}")
def get_scan(request: Request, scan_id: str):
    sup = _sup(request)
    if not sup or sup.scan_state["id"] != scan_id:
        raise ApiError(404, "NOT_FOUND", "照合が見つかりません（再起動で消えた結果は未確認です）")
    return _json({"scan_id": scan_id, "running": sup.scan_state["running"], "result": sup.scan_state["result"]})


@router.post("/system/scans/{scan_id}/cancel")
def cancel_scan(request: Request, scan_id: str):
    sup = _sup(request)
    if not sup or sup.scan_state["id"] != scan_id:
        raise ApiError(404, "NOT_FOUND", "照合が見つかりません")
    sup.cancel_scan()
    return _json({"accepted": True}, 202)
