"""配信セッション・ジョブ・セグメント・試行予算の業務操作（DBアクセスは短い取引）。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, func, select, update

from live_rec_lab import storage
from live_rec_lab.db.models import (
    collection_gaps,
    comment_collection_jobs,
    recording_jobs,
    recording_segments,
    retry_budgets,
    stream_sessions,
    system_events,
)
from live_rec_lab.runtime import utcnow


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def fmt_ts(d: datetime | None) -> str | None:
    return None if d is None else d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def add_event(conn: Connection, event_type: str, severity: str, message: str, session_id: int | None = None,
              metadata: dict | None = None) -> None:
    conn.execute(system_events.insert().values(
        event_type=event_type, severity=severity, stream_session_id=session_id, message=message,
        metadata=json.dumps(metadata, ensure_ascii=False) if metadata else None, created_at=utcnow()))


# ---------------------------------------------------------------- sessions
ACTIVE = ("LIVE", "ENDING")


def get_session(conn: Connection, session_id: int) -> dict:
    return dict(conn.execute(select(stream_sessions).where(stream_sessions.c.id == session_id)).mappings().one())


def find_session(conn: Connection, streamer_id: int, stream_id: str | None) -> dict | None:
    q = select(stream_sessions).where(stream_sessions.c.streamer_id == streamer_id)
    if stream_id:
        q = q.where(stream_sessions.c.platform_stream_id == stream_id)
    else:
        q = q.where(stream_sessions.c.status.in_(ACTIVE))
    row = conn.execute(q.order_by(stream_sessions.c.id.desc())).mappings().first()
    return dict(row) if row else None


def open_session(conn: Connection, streamer: dict, stream_id: str | None, title: str | None,
                 started_at: datetime | None, recording_root: Path) -> tuple[dict, bool]:
    """既存の同一配信なら再利用（二重セッションを作らない）。(session, created)"""
    now = utcnow()
    existing = find_session(conn, streamer["id"], stream_id)
    if existing is not None:
        if existing["status"] in ("LIVE", "ENDING"):
            conn.execute(update(stream_sessions).where(stream_sessions.c.id == existing["id"]).values(
                status="LIVE", last_seen_live_at=now, updated_at=now))
            return get_session(conn, existing["id"]), False
        # 終了済み: 終了未確認で収束していた場合のみ同一セッションを再開できる
        if existing["end_reason"] == "unconfirmed":
            conn.execute(update(stream_sessions).where(stream_sessions.c.id == existing["id"]).values(
                status="LIVE", ended_at=None, end_reason=None, manual_convergence_at=None,
                collection_converged_at=None, last_seen_live_at=now, updated_at=now))
            return get_session(conn, existing["id"]), False
        return existing, False
    res = conn.execute(stream_sessions.insert().values(
        streamer_id=streamer["id"], platform_stream_id=stream_id, title=title, platform_started_at=fmt_ts(started_at),
        detected_at=now, status="LIVE", last_seen_live_at=now, recording_root=str(recording_root),
        created_at=now, updated_at=now))
    return get_session(conn, res.inserted_primary_key[0]), True


def set_status(conn: Connection, session_id: int, status: str, **extra) -> None:
    conn.execute(update(stream_sessions).where(stream_sessions.c.id == session_id).values(
        status=status, updated_at=utcnow(), **extra))


def touch_live(conn: Connection, session_id: int, title: str | None = None) -> None:
    vals = {"last_seen_live_at": utcnow(), "updated_at": utcnow()}
    if title:
        vals["title"] = title
    conn.execute(update(stream_sessions).where(stream_sessions.c.id == session_id).values(**vals))


def ensure_naming(conn: Connection, session_id: int, tz_name: str) -> dict:
    """最初の正式名予約時に命名用の固定値を確定する（以後は再計算しない）。"""
    s = get_session(conn, session_id)
    if s["filename_datetime"]:
        return s
    base = parse_ts(s["platform_started_at"]) or parse_ts(s["detected_at"])
    conn.execute(update(stream_sessions).where(stream_sessions.c.id == session_id).values(
        filename_datetime=storage.filename_datetime(base, tz_name),
        filename_title=storage.safe_component(s["title"] or "", fallback="untitled"),
        updated_at=utcnow()))
    s = get_session(conn, session_id)
    s["_date_folder"] = storage.date_folder(base, tz_name)
    return s


def date_folder_for(session: dict, tz_name: str) -> str:
    """日付フォルダは固定した命名日時（filename_datetime）から決める。"""
    d = session["filename_datetime"]
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


# ---------------------------------------------------------------- jobs
def create_recording_job(conn: Connection, session_id: int, budget_id: int | None) -> int:
    now = utcnow()
    return conn.execute(recording_jobs.insert().values(
        stream_session_id=session_id, status="PENDING", retry_budget_id=budget_id, created_at=now, updated_at=now,
        started_at=now)).inserted_primary_key[0]


def update_job(conn: Connection, table, job_id: int, **vals) -> None:
    conn.execute(update(table).where(table.c.id == job_id).values(updated_at=utcnow(), **vals))


def create_comment_job(conn: Connection, session_id: int, budget_id: int | None) -> int:
    now = utcnow()
    return conn.execute(comment_collection_jobs.insert().values(
        stream_session_id=session_id, status="PENDING", retry_budget_id=budget_id, started_at=now,
        created_at=now, updated_at=now)).inserted_primary_key[0]


def add_gap(conn: Connection, session_id: int, job_type: str, started_at: str | None, ended_at: str | None,
            reason: str, *, start_basis: str = "measured", end_basis: str = "measured", status: str = "closed",
            recovery_key: str | None = None) -> None:
    conn.execute(collection_gaps.insert().values(
        stream_session_id=session_id, job_type=job_type, started_at=started_at, ended_at=ended_at, reason=reason,
        recovery_key=recovery_key, start_basis=start_basis, end_basis=end_basis, gap_status=status,
        created_at=utcnow()))


def finish_interruption(conn: Connection, session_id: int, job_type: str, started_at: str, *, confirmed: bool) -> None:
    last_live = conn.execute(select(stream_sessions.c.last_seen_live_at).where(
        stream_sessions.c.id == session_id)).scalar_one()
    # 終了確認が遅れても、切断後の配信継続を確認できなければ末尾の空試行を欠損として確定しない。
    if not confirmed or (last_live and last_live > started_at):
        add_gap(conn, session_id, job_type, started_at, None, "interrupted",
                start_basis="approximate", end_basis="unknown")


# ---------------------------------------------------------------- budgets (§6.5.2)
def budget_for(conn: Connection, session_id: int, kind: str, max_extra: int, *, new_generation: bool = False) -> int:
    """最新世代の予算を返す。設定値は新世代の作成時に固定し、再起動・再検知で復活させない。"""
    row = conn.execute(select(retry_budgets).where(
        retry_budgets.c.stream_session_id == session_id, retry_budgets.c.kind == kind
    ).order_by(retry_budgets.c.generation.desc())).mappings().first()
    if row is not None and not new_generation:
        return row["id"]
    gen = (row["generation"] + 1) if row else 1
    return conn.execute(retry_budgets.insert().values(
        stream_session_id=session_id, kind=kind, generation=gen, max_extra=max_extra, created_at=utcnow()
    )).inserted_primary_key[0]


def reserve_attempt(conn: Connection, budget_id: int, attempt_id: str) -> str | None:
    """接続前に同一取引で予約。初回は追加予算を消費しない。返値: 'initial'/'extra'、不可ならNone。"""
    b = conn.execute(select(retry_budgets).where(retry_budgets.c.id == budget_id)).mappings().one()
    if b["suppression"]:
        return None
    if not b["initial_reserved"]:
        conn.execute(update(retry_budgets).where(retry_budgets.c.id == budget_id).values(
            initial_reserved=True, last_attempt_id=attempt_id))
        return "initial"
    if b["used_extra"] >= b["max_extra"]:
        conn.execute(update(retry_budgets).where(retry_budgets.c.id == budget_id).values(suppression="exhausted"))
        return None
    conn.execute(update(retry_budgets).where(retry_budgets.c.id == budget_id).values(
        used_extra=b["used_extra"] + 1, last_attempt_id=attempt_id))
    return "extra"


def suppression_of(conn: Connection, session_id: int, kind: str) -> str | None:
    """最新世代の抑止理由（枯渇・手動停止・制限など）。設定変更や再検知では解除されない（要求§6.5.3）。"""
    row = conn.execute(select(retry_budgets.c.suppression).where(
        retry_budgets.c.stream_session_id == session_id, retry_budgets.c.kind == kind
    ).order_by(retry_budgets.c.generation.desc())).first()
    return row[0] if row else None


def suppress(conn: Connection, budget_id: int, reason: str) -> None:
    conn.execute(update(retry_budgets).where(retry_budgets.c.id == budget_id).values(suppression=reason))


# ---------------------------------------------------------------- segments
def add_pending_segment(conn: Connection, job_id: int, session_id: int, engine: str, tmp_path: str,
                        attempt_id: str) -> int:
    now = utcnow()
    return conn.execute(recording_segments.insert().values(
        recording_job_id=job_id, stream_session_id=session_id, recorder_engine=engine, attempt_id=attempt_id,
        started_at=now, temp_path=tmp_path, validation_status="pending", created_at=now)).inserted_primary_key[0]


def _taken(root: Path, directory: Path, stem: str) -> bool:
    """正式名（動画・全字幕）が既存と大小文字無視で衝突するか。"""
    if not directory.is_dir():
        return False
    names = {n.casefold() for n in os.listdir(directory)}
    return any((stem + suf).casefold() in names for suf in storage.SUFFIXES.values())


def reserve_number_and_name(conn: Connection, session_id: int, root: Path, streamer_dir: str, tz_name: str,
                            platform: str) -> tuple[int, str, Path]:
    """保持すべき実データがあると判定した時点で、番号とstemを同一取引で確定する（空試行は採番しない）。"""
    s = ensure_naming(conn, session_id, tz_name)
    date_dir = date_folder_for(s, tz_name)
    directory = root / streamer_dir / date_dir
    room = storage.stem_room(root, streamer_dir, date_dir)
    number = s["next_segment_number"]
    disamb = s["filename_disambiguator"]
    while True:
        stem = storage.build_stem(platform, streamer_dir, s["filename_datetime"], s["filename_title"], number, disamb,
                                  room=room)
        if disamb is None and _taken(root, directory, stem):
            disamb = storage.session_token(session_id)
            continue
        if _taken(root, directory, stem):
            disamb = disamb + "x"  # 延長して再予約
            continue
        break
    conn.execute(update(stream_sessions).where(stream_sessions.c.id == session_id).values(
        next_segment_number=number + 1, filename_disambiguator=disamb, updated_at=utcnow()))
    return number, stem, directory


def segments_of(conn: Connection, session_id: int) -> list[dict]:
    q = select(recording_segments).where(recording_segments.c.stream_session_id == session_id).order_by(
        recording_segments.c.segment_number, recording_segments.c.id)
    return [dict(r) for r in conn.execute(q).mappings()]


def session_counts(conn: Connection, session_id: int) -> dict:
    from live_rec_lab.db.models import comments

    n = conn.execute(select(func.count()).select_from(comments).where(comments.c.stream_session_id == session_id)).scalar()
    return {"comments": n}
