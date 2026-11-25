"""アーカイブ削除（アーカイブ全体のみ）・手動収束・容量/期限判定・ストレージ照合（要求§10.4〜10.5）。

削除は「最小記録（復元対象外）→ 管理ファイルの世代確認つき削除 → 全ファイル確認後にDB一括削除」の順で、
世代を証明できない対象は保留してDBを残す。稼働中・未確定・復旧中・字幕処理中は削除しない。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, delete, select, update

from live_rec_lab import storage
from live_rec_lab.config import Environment
from live_rec_lab.db.models import (
    collection_gaps,
    comment_collection_jobs,
    comments,
    deletion_operations,
    recording_jobs,
    recording_segments,
    retry_budgets,
    stream_sessions,
    streamers,
    system_events,
)
from live_rec_lab.runtime import utcnow

log = logging.getLogger("live_rec_lab.archives")
ACTIVE_JOB = ("PENDING", "STARTING", "RECORDING", "RECONNECTING", "FINALIZING", "CONNECTING", "COLLECTING")


class Refused(Exception):
    """削除不可（409）。"""


def tombstone_file(env: Environment) -> Path:
    return env.state_dir / "deleted-archives.jsonl"  # 復元対象外（DBの外）


def was_deleted(env: Environment, platform: str, account: str, stream_id: str | None) -> bool:
    f = tombstone_file(env)
    if not stream_id or not f.exists():
        return False
    for line in f.read_text("utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("platform") == platform and r.get("account") == account and r.get("stream_id") == stream_id:
            return True
    return False


def manual_converge(engine: Engine, session_id: int, reason: str, running: bool) -> None:
    """終了未確認アーカイブの手動収束（サービス上の終了確認やended_atの捏造ではない）。"""
    if not reason.strip():
        raise Refused("理由を入力してください")
    with engine.begin() as conn:
        s = conn.execute(select(stream_sessions).where(stream_sessions.c.id == session_id)).mappings().first()
        if s is None:
            raise LookupError
        if running or s["status"] != "ENDED" or s["end_reason"] != "unconfirmed":
            raise Refused("終了未確認で収束済みのセッションのみ手動収束できます（稼働中は不可）")
        _ensure_idle(conn, session_id)
        conn.execute(update(stream_sessions).where(stream_sessions.c.id == session_id).values(
            manual_convergence_at=utcnow(), updated_at=utcnow()))
        conn.execute(system_events.insert().values(event_type="session.manual_convergence", severity="info",
                                                   stream_session_id=session_id, message=f"手動収束: {reason.strip()[:200]}",
                                                   created_at=utcnow()))


def _ensure_idle(conn, session_id: int) -> None:
    busy = conn.execute(select(recording_jobs.c.id).where(recording_jobs.c.stream_session_id == session_id,
                                                          recording_jobs.c.status.in_(ACTIVE_JOB))).first()
    busy2 = conn.execute(select(comment_collection_jobs.c.id).where(
        comment_collection_jobs.c.stream_session_id == session_id, comment_collection_jobs.c.status.in_(ACTIVE_JOB))).first()
    pend = conn.execute(select(recording_segments.c.id).where(recording_segments.c.stream_session_id == session_id,
                                                              recording_segments.c.validation_status == "pending")).first()
    speech = conn.execute(select(recording_segments.c.id).where(recording_segments.c.stream_session_id == session_id,
                                                                recording_segments.c.speech_status == "running")).first()
    if busy or busy2 or pend or speech:
        raise Refused("録画・コメント・保存・字幕処理が残っているため操作できません")


def deletable(conn, session_id: int, *, automatic: bool = False) -> dict:
    s = conn.execute(select(stream_sessions).where(stream_sessions.c.id == session_id)).mappings().first()
    if s is None:
        raise LookupError
    s = dict(s)
    if s["status"] != "ENDED":
        raise Refused("稼働中のアーカイブは削除できません")
    _ensure_idle(conn, session_id)
    if s["end_reason"] != "confirmed":
        if automatic:
            raise Refused("終了未確認のアーカイブは自動削除しません")
        if not s["manual_convergence_at"]:
            raise Refused("終了未確認のため、詳細画面で手動収束を確定してから削除してください")
    op = conn.execute(select(deletion_operations).where(deletion_operations.c.target_type == "session",
                                                        deletion_operations.c.target_id == session_id,
                                                        deletion_operations.c.status.in_(("準備中", "実行中")))).first()
    if op:
        raise Refused("このアーカイブは削除処理中です")
    return s


def _entries(conn, sess: dict) -> list[dict]:
    root = sess["recording_root"]
    out = []
    for seg in conn.execute(select(recording_segments).where(
            recording_segments.c.stream_session_id == sess["id"])).mappings():
        ids = json.loads(seg["file_identity"] or "{}")
        for kind, rel, ident in (("video", seg["file_path"], {k: ids.get(k) for k in ("dev", "ino", "size", "mtime_ns")}),
                                 ("comment_srt", seg["srt_path"], ids.get("srt")),
                                 ("speech_srt", seg["speech_srt_path"], ids.get("speech")),
                                 ("ass", seg["ass_path"], ids.get("ass"))):
            if rel:
                out.append({"segment_id": seg["id"], "kind": kind, "root": root, "rel": rel, "identity": ident,
                            "state": "pending"})
        if seg["temp_path"]:  # 保全された未確定の一時ファイル（本製品の一時名のみ）
            out.append({"segment_id": seg["id"], "kind": "temp", "root": root, "abs": seg["temp_path"], "identity": None,
                        "state": "pending"})
    return out


def _delete_entry(e: dict) -> None:
    if e["kind"] == "temp":
        p = Path(e["abs"])
        if not p.name.endswith(storage.TMP_SUFFIX):
            e["state"], e["reason"] = "held", "本製品の一時ファイルと確認できません"
            return
        root = Path(e["root"])
        if not root.is_dir():
            e["state"], e["reason"] = "held", "保存ルートに接続できません"
            return
        if p.exists():
            p.unlink()
        e["state"] = "deleted"
        return
    root = Path(e["root"])
    if not root.is_dir():
        e["state"], e["reason"] = "held", "保存ルートに接続できません（不在と扱いません）"
        return
    p = root / e["rel"]
    if not p.exists():
        e["state"] = "absent"
        return
    if p.is_symlink() or not p.is_file():
        e["state"], e["reason"] = "held", "通常ファイルではありません"
        return
    ident = e.get("identity")
    if not ident or not storage.same_identity(storage.identity(p), ident, check_mtime=False):
        e["state"], e["reason"] = "held", "記録した世代と一致しません（外部で置換された可能性）"
        return
    p.unlink()
    e["state"] = "deleted"


def delete_archive(engine: Engine, env: Environment, session_id: int, trigger: str = "manual", *, automatic: bool = False) -> dict:
    with engine.begin() as conn:
        sess = deletable(conn, session_id, automatic=automatic)
        st = conn.execute(select(streamers).where(streamers.c.id == sess["streamer_id"])).mappings().one()
        entries = _entries(conn, sess)
        op_id = conn.execute(deletion_operations.insert().values(
            target_type="session", target_id=session_id, trigger=trigger, status="準備中",
            target_paths=json.dumps(sorted({e.get("rel") or e.get("abs") for e in entries}, key=str), ensure_ascii=False),
            target_entries=json.dumps(entries, ensure_ascii=False), started_at=utcnow())).inserted_primary_key[0]
    # 復元対象外の最小記録（DB削除前）。コメント本文・投稿者等は残さない
    tomb = tombstone_file(env)
    tomb.parent.mkdir(parents=True, exist_ok=True)
    with tomb.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"platform": st["platform"], "account": st["platform_user_id"],
                            "stream_id": sess["platform_stream_id"], "session_id": session_id, "deleted_at": utcnow(),
                            "op": op_id}) + "\n")
    with engine.begin() as conn:
        conn.execute(update(deletion_operations).where(deletion_operations.c.id == op_id).values(status="実行中"))
    for e in entries:
        try:
            _delete_entry(e)
        except OSError as exc:
            e["state"], e["reason"] = "failed", str(exc)
    held = [e for e in entries if e["state"] in ("held", "failed")]
    with engine.begin() as conn:
        conn.execute(update(deletion_operations).where(deletion_operations.c.id == op_id).values(
            target_entries=json.dumps(entries, ensure_ascii=False)))
        if held:
            conn.execute(update(deletion_operations).where(deletion_operations.c.id == op_id).values(
                status="一部失敗", last_error=held[0].get("reason"), finished_at=utcnow()))
            return {"operation_id": op_id, "status": "partial", "held": len(held), "reason": held[0].get("reason")}
        # 全管理ファイルの削除/不在確認後に関連DBを一括削除
        for tbl in (comments, collection_gaps, comment_collection_jobs):
            conn.execute(delete(tbl).where(tbl.c.stream_session_id == session_id))
        conn.execute(delete(system_events).where(system_events.c.stream_session_id == session_id))
        conn.execute(delete(recording_segments).where(recording_segments.c.stream_session_id == session_id))
        conn.execute(delete(recording_jobs).where(recording_jobs.c.stream_session_id == session_id))
        conn.execute(delete(retry_budgets).where(retry_budgets.c.stream_session_id == session_id))
        conn.execute(delete(stream_sessions).where(stream_sessions.c.id == session_id))
        conn.execute(update(deletion_operations).where(deletion_operations.c.id == op_id).values(
            status="完了", finished_at=utcnow()))
    _prune_empty_dirs(entries)
    return {"operation_id": op_id, "status": "completed", "deleted": sum(1 for e in entries if e["state"] == "deleted")}


def _prune_empty_dirs(entries: list[dict]) -> None:
    """空になった日付/配信者フォルダだけを除去（管理外ファイルがあれば残す）。"""
    for e in entries:
        if "rel" not in e:
            continue
        root = Path(e["root"])
        d = (root / e["rel"]).parent
        for _ in range(2):
            try:
                if d != root and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                break
            d = d.parent


# ---------------------------------------------------------------- 容量・期限
def managed_bytes(engine: Engine) -> int:
    from sqlalchemy import func

    with engine.begin() as conn:
        return conn.execute(select(func.coalesce(func.sum(recording_segments.c.file_size), 0)).where(
            recording_segments.c.validation_status == "valid")).scalar() or 0


def capacity_ok(engine: Engine, root: Path, st: dict) -> tuple[bool, str | None]:
    """新規録画の可否。判定不能は無制限とせず不可にする。"""
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        return False, f"保存先の容量を確認できません: {exc}"
    need = int(float(st["min_free_gib"]) * 1024**3 + 0.999)
    if free < need:
        return False, f"空き容量が最低値を下回っています（{free // 1024**3} GiB < {st['min_free_gib']} GiB）"
    cap = float(st["max_recording_gib"])
    if cap > 0 and managed_bytes(engine) >= int(cap * 1024**3):
        return False, "保存容量の上限に達しています"
    return True, None


def expired_candidates(engine: Engine, days: int) -> list[int]:
    if days <= 0:
        return []
    limit = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with engine.begin() as conn:
        return [r[0] for r in conn.execute(select(stream_sessions.c.id).where(
            stream_sessions.c.status == "ENDED", stream_sessions.c.end_reason == "confirmed",
            stream_sessions.c.ended_at < limit).order_by(stream_sessions.c.ended_at))]


def oldest_deletable(engine: Engine) -> list[int]:
    with engine.begin() as conn:
        return [r[0] for r in conn.execute(select(stream_sessions.c.id).where(
            stream_sessions.c.status == "ENDED", stream_sessions.c.end_reason == "confirmed"
        ).order_by(stream_sessions.c.ended_at, stream_sessions.c.id))]


# ---------------------------------------------------------------- ストレージ照合（読取専用）
def scan_storage(engine: Engine, root: Path, should_cancel=lambda: False, max_items: int = 200000) -> dict:
    """DB登録ファイルの実在・サイズ、未登録メディア、一時ファイルを読取専用で照合する。録画中の増加は未確認扱い。"""
    result = {"started_at": utcnow(), "ok": 0, "missing": [], "size_mismatch": [], "unregistered": [], "temp": [],
              "unconfirmed": [], "total_registered": 0, "cancelled": False, "root_ok": root.is_dir()}
    if not root.is_dir():
        result["error"] = "保存ルートに接続できません（未確認）"
        result["finished_at"] = utcnow()
        return result
    registered: set[str] = set()
    with engine.begin() as conn:
        rows = [dict(r) for r in conn.execute(select(recording_segments).where(
            recording_segments.c.validation_status == "valid"))]
        running = {r[0] for r in conn.execute(select(stream_sessions.c.id).where(stream_sessions.c.status.in_(("LIVE", "ENDING"))))}
    for seg in rows:
        if should_cancel():
            result["cancelled"] = True
            break
        result["total_registered"] += 1
        for rel in (seg["file_path"], seg["srt_path"], seg["speech_srt_path"], seg["ass_path"]):
            if rel:
                registered.add(rel.casefold())
        p = root / seg["file_path"]
        if seg["stream_session_id"] in running:
            result["unconfirmed"].append(seg["file_path"])
        elif not p.exists():
            result["missing"].append(seg["file_path"])
        elif p.stat().st_size != seg["file_size"]:
            result["size_mismatch"].append(seg["file_path"])
        else:
            result["ok"] += 1
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            n += 1
            if n > max_items or should_cancel():
                result["cancelled"] = result["cancelled"] or should_cancel()
                break
            rel = str((Path(dirpath) / f).relative_to(root))
            if f.endswith(storage.TMP_SUFFIX):
                result["temp"].append(rel)
            elif rel.casefold() not in registered and f.lower().endswith((".mp4", ".ts", ".flv", ".srt", ".ass")):
                result["unregistered"].append(rel)
    result["finished_at"] = utcnow()
    return result
