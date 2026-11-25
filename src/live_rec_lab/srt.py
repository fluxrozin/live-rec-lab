"""コメントSRT生成（要求§7.7〜7.8）。セグメントの0秒を基準にする。"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import Connection, select

from live_rec_lab.db.models import comments
from live_rec_lab.sessions import parse_ts

_TC_LIKE = re.compile(r"^(\s*)(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->.*)$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def tc(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _one_line(s: str | None) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub("", (s or "").replace("\r\n", "\n").replace("\r", "\n"))).strip()


def author_label(nickname: str | None, user_id: str | None, is_anonymous: bool | None, label: str | None) -> str:
    if is_anonymous:
        return f" (匿名コメント#{label})" if label else " (匿名コメント)"  # 利用者指定の表記（2026-11-25）
    name, uid = _one_line(nickname), _one_line(user_id).lstrip("@")
    if name and uid:
        return f" ({name} | @{uid})"
    if name:
        return f" ({name})"
    if uid:
        return f" (@{uid})"
    return " (投稿者不明)"


def body_text(message: str, nickname, user_id, is_anonymous, label) -> str:
    """本文（改行は保持、空行は除去、タイムコード風の行は構造を壊さないよう安全化）+ 投稿者表記。"""
    text = _CTRL.sub("", (message or "").replace("\r\n", "\n").replace("\r", "\n"))
    lines = [ln for ln in (x.rstrip() for x in text.split("\n")) if ln.strip()]
    lines = [_TC_LIKE.sub(lambda m: m.group(1) + m.group(2).replace("-->", "→"), ln) for ln in lines]
    suffix = author_label(nickname, user_id, is_anonymous, label)
    if not lines:
        return suffix.strip()
    lines[-1] += suffix
    return "\n".join(lines)


def build_cues(rows: list[dict], media_start, duration_ms: int, display_ms: int, offset_ms: int) -> list[tuple[int, int, str]]:
    cues: list[tuple[int, int, str]] = []
    for r in rows:
        eff = parse_ts(r["effective_at"])
        start = int((eff - media_start).total_seconds() * 1000) + offset_ms
        if start < 0 or start >= duration_ms:
            continue  # 録画範囲外は誤った動画時刻へ押し込まない
        end = min(start + display_ms, duration_ms)
        if end <= start:
            continue
        cues.append((start, end, body_text(r["message"], r["nickname"], r["platform_user_id"], r["is_anonymous"],
                                           r["anonymous_label"])))
    return cues


def render(cues: list[tuple[int, int, str]]) -> str:
    return "".join(f"{i}\n{tc(a)} --> {tc(b)}\n{text}\n\n" for i, (a, b, text) in enumerate(cues, 1))


def media_start_of(seg: dict):
    """動画0秒の絶対時刻と根拠。実測(first_media_at)優先、なければ録画開始の近似。"""
    if seg.get("first_media_at"):
        return parse_ts(seg["first_media_at"]), seg.get("sync_method") or "measured"
    return parse_ts(seg["started_at"]), "approx_process_start"


def comments_for_window(conn: Connection, session_id: int, start, end) -> list[dict]:
    from live_rec_lab.sessions import fmt_ts

    lo, hi = fmt_ts(start - timedelta(seconds=1)), fmt_ts(end + timedelta(seconds=1))
    q = select(comments).where(comments.c.stream_session_id == session_id, comments.c.effective_at >= lo,
                               comments.c.effective_at <= hi).order_by(comments.c.effective_at, comments.c.id)
    return [dict(r) for r in conn.execute(q).mappings()]


def segment_srt(conn: Connection, seg: dict, display_ms: int, manual_offset_ms: int) -> tuple[str, int, str]:
    """(SRT文字列, cue数, 同期方式)。同期基準がなければ ValueError。"""
    if seg["validation_status"] != "valid" or not seg.get("duration_ms"):
        raise ValueError("検証済みの動画がありません")
    start, method = media_start_of(seg)
    if start is None:
        raise ValueError("同期基準がありません")
    end = start + timedelta(milliseconds=seg["duration_ms"] + abs(manual_offset_ms))
    rows = comments_for_window(conn, seg["stream_session_id"], start - timedelta(milliseconds=abs(manual_offset_ms)), end)
    cues = build_cues(rows, start, seg["duration_ms"], display_ms, manual_offset_ms)
    return render(cues), len(cues), method
