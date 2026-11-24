"""Presentation data for the isolated operations views."""

import json
import os
import re
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..ass_jobs import latest_ass
from ..isolation import Workspace, redact_log_text
from ..publication import matches_media
from ..retention import media_deletion_status
from ..speech_jobs import latest_speech, published_speech_uses_job
from .queries import like_pattern, page_rows, session_select

_TIME_RANGE = re.compile(r"(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})")
SPEECH_JOB_STATES = {
    "not_started": "未投入",
    "queued": "待機",
    "running": "処理中",
    "completed": "完了",
    "no_speech": "発話なし",
    "not_applicable": "音声なし",
    "blocked": "保留",
    "failed": "失敗",
    "needs_review": "要確認",
    "interrupted": "中断",
    "deleted": "削除済み",
}
ASS_JOB_STATES = {
    "not_started": "未投入",
    "queued": "待機",
    "running": "生成中",
    "completed": "完了",
    "provisional": "暫定",
    "partial": "片側または欠損あり",
    "skipped": "対象なし",
    "blocked": "保留",
    "failed": "失敗",
    "needs_review": "要確認",
    "interrupted": "中断",
    "expired": "期限削除済み",
    "deleted": "削除済み",
}
_STATES = {
    "stream": {
        "live": ("配信中", "live"),
        "ending": ("終了処理中", "warning"),
        "ended": ("終了", "neutral"),
        "offline": ("オフライン", "neutral"),
        "interrupted": ("中断", "warning"),
        "restricted": ("視聴制限", "warning"),
    },
    "recording": {
        "running": ("録画中", "recording"),
        "completed": ("保存済み", "live"),
        "partial": ("一部保存", "warning"),
        "cancelled": ("停止済み", "neutral"),
        "deleted": ("削除済み", "neutral"),
        "blocked": ("保留", "warning"),
        "pending": ("待機", "neutral"),
        "failed": ("録画失敗", "danger"),
        "needs_review": ("要確認", "warning"),
        "interrupted": ("中断", "warning"),
        "not_started": ("未録画", "neutral"),
    },
    "comment": {
        "running": ("収集中", "comment"),
        "completed": ("収集済み", "live"),
        "pending": ("待機", "neutral"),
        "failed": ("収集失敗", "danger"),
        "interrupted": ("中断", "warning"),
        "partial": ("一部取得", "warning"),
        "blocked": ("保留", "warning"),
        "disabled": ("無効", "neutral"),
        "not_started": ("未収集", "neutral"),
        "expired": ("期限削除済み", "neutral"),
    },
    "subtitle": {
        "completed": ("保存済み", "live"),
        "queued": ("待機", "neutral"),
        "running": ("生成中", "comment"),
        "interrupted": ("中断", "warning"),
        "provisional": ("暫定版", "warning"),
        "deleted": ("削除済み", "neutral"),
        "pending": ("待機", "neutral"),
        "failed": ("生成失敗", "danger"),
        "partial": ("一部取得", "warning"),
        "needs_review": ("要確認", "warning"),
        "blocked": ("保留", "warning"),
        "skipped": ("対象なし", "neutral"),
        "expired": ("期限削除済み", "neutral"),
        "not_started": ("未生成", "neutral"),
    },
}


def presentation_status(kind: str, value: str | None) -> tuple[str, str]:
    return _STATES[kind].get(value, ("未確認", "neutral"))


def platform_label(value: str) -> str:
    return {"tiktok": "TikTok LIVE", "twitcasting": "ツイキャス"}.get(value, value)


def local_time(value: str | None, timezone: str) -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo:
                return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y/%m/%d %H:%M:%S")
        except ValueError:
            pass
    return "未確認"


def human_size(value: int | None) -> str:
    if value is None:
        return "未確認"
    if value < 1024:
        return f"{value} B"
    for divisor, label in ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB")):
        if value >= divisor:
            return f"{value / divisor:,.1f} {label}"
    return "未確認"


def active_snapshot(database: sqlite3.Connection) -> dict:
    counts = database.execute("""
        SELECT (SELECT count(*) FROM streamers WHERE archived_at IS NULL AND enabled = 1) AS monitored,
               count(*) FILTER (WHERE stream_state = 'live') AS live,
               count(*) FILTER (WHERE recording_state = 'running') AS recording,
               count(*) FILTER (WHERE comment_state = 'running') AS collecting
        FROM stream_sessions
    """).fetchone()
    running = database.execute(
        session_select
        + " WHERE stream_state IN ('live', 'ending') ORDER BY started_at DESC, stream_sessions.id DESC LIMIT 25"
    ).fetchall()
    return {"counts": counts, "running": running, "now": datetime.now(UTC).isoformat()}


def disk_summary(workspace: Workspace) -> tuple[int | None, int | None]:
    try:
        volume = os.statvfs(workspace.recordings)
        return volume.f_bavail * volume.f_frsize, volume.f_blocks * volume.f_frsize
    except OSError:
        return None, None


def verified_outputs(
    database: sqlite3.Connection,
    workspace: Workspace,
    session_id: int,
    *,
    segment_id: int | None = None,
) -> list[dict]:
    segments = database.execute(
        "SELECT * FROM recording_segments WHERE session_id = ? AND (? IS NULL OR id = ?) ORDER BY number",
        (session_id, segment_id, segment_id),
    ).fetchall()
    expiration = database.execute(
        "SELECT state FROM comment_expirations WHERE session_id = ?", (session_id,)
    ).fetchone()
    deletion = media_deletion_status(database, session_id)
    results = []
    for segment in segments:
        available = not deletion and matches_media(workspace, segment["video_path"], segment["video_generation"])
        publications = database.execute(
            "SELECT * FROM subtitle_publications WHERE segment_id = ?", (segment["id"],)
        ).fetchall()
        statuses = {}
        speech_origin = None
        ass_attempt = latest_ass(database, segment["id"])
        for record in publications:
            if record["kind"] == "speech":
                speech_origin = (
                    "ローカル認識"
                    if published_speech_uses_job(database, segment["id"], record["input_version"])
                    else "架空発話"
                )
            statuses[record["kind"]] = (
                "completed"
                if available and matches_media(workspace, record["relative_path"], record["file_generation"])
                else "needs_review"
            )
            if (
                record["kind"] == "ass"
                and statuses["ass"] == "completed"
                and ass_attempt
                and ass_attempt.id == record["input_version"]
                and ass_attempt.state in {"partial", "provisional"}
            ):
                statuses["ass"] = ass_attempt.state
        if expiration:
            statuses.update(
                dict.fromkeys(("comment", "ass"), "expired" if expiration["state"] == "expired" else "blocked")
            )
        if deletion:
            statuses.update(
                dict.fromkeys(
                    ("comment", "speech", "ass"), "deleted" if deletion["state"] == "completed" else "blocked"
                )
            )
        results.append(
            {
                "id": segment["id"],
                "number": segment["number"],
                "duration": segment["duration_ms"],
                "video_name": Path(segment["video_path"]).name,
                "available": available,
                "statuses": statuses,
                "speech_origin": speech_origin,
                "speech_attempt": latest_speech(database, segment["id"]),
                "ass_attempt": ass_attempt,
            }
        )
    return results


def speech_queue_page(
    database: sqlite3.Connection,
    workspace: Workspace,
    *,
    platform: str,
    term: str,
    session_id: int | None,
    speech_state: str,
    ass_state: str,
    page: int,
    page_size: int,
) -> dict:
    tables = {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_master WHERE name IN ('speech_attempts','ass_attempts')")
    }

    def latest(table: str) -> str:
        if table not in tables:
            return "'not_started'"
        select = f"SELECT state FROM {table} WHERE segment_id = segment.id ORDER BY id DESC LIMIT 1"
        return f"coalesce(({select}), 'not_started')"

    base = f"""
    SELECT session.*, segment.id AS segment_id,
        CASE WHEN session.recording_state = 'deleted' THEN 'deleted'
             ELSE {latest("speech_attempts")} END AS speech_state,
        CASE WHEN session.recording_state = 'deleted' THEN 'deleted'
             WHEN expiration.state = 'expired' THEN 'expired'
             WHEN expiration.state IS NOT NULL THEN 'blocked'
             ELSE {latest("ass_attempts")} END AS ass_state
    FROM ({session_select}) session
    JOIN recording_segments segment ON segment.session_id = session.id
    LEFT JOIN comment_expirations expiration ON expiration.session_id = session.id
    WHERE (? = '' OR platform = ?) AND (? IS NULL OR session.id = ?)
      AND (? = '' OR title LIKE ? ESCAPE '!' OR normalized_id LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!')
    """
    search = like_pattern(term)
    filters = (platform, platform, session_id, session_id, term, search, search, search)
    counts = {"speech": {}, "ass": {}}
    for row in database.execute(
        f"SELECT speech_state, ass_state, count(*) FROM ({base}) GROUP BY speech_state, ass_state", filters
    ):
        for category, state in zip(counts, row[:2], strict=True):
            counts[category][state] = counts[category].get(state, 0) + row[2]
    result = page_rows(
        database,
        f"SELECT * FROM ({base}) WHERE (? = '' OR speech_state = ?) AND (? = '' OR ass_state = ?)",
        (*filters, speech_state, speech_state, ass_state, ass_state),
        "started_at DESC, id DESC, segment_id",
        page,
        page_size,
    )
    for row in result["items"]:
        (row["segment"],) = verified_outputs(database, workspace, row["id"], segment_id=row["segment_id"])
    return {**result, "counts": counts}


def speech_cues(
    database: sqlite3.Connection,
    workspace: Workspace,
    session_id: int | None,
    term: str,
    page: int,
    order: str,
    *,
    segment_id: int | None = None,
) -> tuple[list[dict], bool, bool]:
    order_sql = "ASC" if order == "asc" else "DESC"
    rows = database.execute(
        f"""
        SELECT subtitle_publications.*, segment.video_path, segment.video_generation, segment.number, session.title,
               session.id AS session_id
        FROM subtitle_publications
        JOIN recording_segments segment ON segment.id = subtitle_publications.segment_id
        JOIN stream_sessions session ON session.id = segment.session_id
        WHERE kind = 'speech' AND (? IS NULL OR session.id = ?) AND (? IS NULL OR segment.id = ?)
        ORDER BY session.started_at {order_sql}, session.id {order_sql}, segment.number {order_sql}
    """,
        (session_id, session_id, segment_id, segment_id),
    )
    results = []
    unavailable = False
    matched = 0
    start = (page - 1) * 25
    for row in rows:
        if media_deletion_status(database, row["session_id"]):
            continue
        if not (
            matches_media(workspace, row["video_path"], row["video_generation"])
            and matches_media(workspace, row["relative_path"], row["file_generation"])
        ):
            unavailable = True
            continue
        try:
            fd = os.open(workspace.media_path(row["relative_path"]), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as source:
                content = source.read(2_000_001)
        except (OSError, UnicodeError):
            unavailable = True
            continue
        if len(content) > 2_000_000 or not matches_media(workspace, row["relative_path"], row["file_generation"]):
            unavailable = True
            continue
        blocks = content.strip().split("\n\n")
        for block in blocks if order == "asc" else reversed(blocks):
            _, separator, text = block.partition("\n")
            times, _, body = text.partition("\n")
            match = _TIME_RANGE.fullmatch(times)
            if not separator or not match or term.casefold() not in body.casefold():
                continue
            if matched >= start:
                if len(results) == 25:
                    return results, True, unavailable
                results.append(
                    {
                        "start": match[1],
                        "end": match[2],
                        "body": body,
                        "session": row,
                        "origin": "ローカル認識"
                        if published_speech_uses_job(database, row["segment_id"], row["input_version"])
                        else "架空発話",
                    }
                )
            matched += 1
    return results, False, unavailable


def fixture_events(workspace: Workspace) -> tuple[list[dict], str]:
    workspace.recheck()
    records = []
    unreadable = False
    state = "available"
    remaining = 512_000
    logs = [workspace.events, *(workspace.events.with_name(f"{workspace.events.name}.{i}") for i in range(1, 5))]
    for path in logs:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(fd)
                if not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()):
                    unreadable = True
                    continue
                if remaining == 0:
                    state = "partial"
                    continue
                size = source.seek(0, os.SEEK_END)
                skipped = max(0, size - remaining)
                source.seek(skipped)
                window = source.read(remaining)
                remaining -= len(window)
                lines = window.splitlines()
                records.extend(reversed(lines[1:] if skipped else lines))
                if skipped:
                    state = "partial"
        except FileNotFoundError:
            continue
        except OSError:
            unreadable = True
    if unreadable:
        state = "partial" if records else "unavailable"
    results = []
    for line in records:
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            state = "partial"
            continue
        if not isinstance(entry, dict) or "timestamp" not in entry:
            state = "partial"
            continue
        item = {key: str(entry.get(key, "")) for key in ("timestamp", "component", "event", "correlation_id")}
        exception = entry.get("exception")
        item["level"] = str(entry.get("level") or ("ERROR" if exception else "記録"))
        item["detail"] = str(exception.get("message", "") if isinstance(exception, dict) else entry.get("message", ""))
        results.append({key: redact_log_text(value) for key, value in item.items()})
    return results, state if results or state != "available" else "empty"
