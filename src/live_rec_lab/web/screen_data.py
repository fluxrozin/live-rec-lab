"""Presentation data for the isolated operations views."""

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..isolation import FixtureWorkspace
from ..publication import matches_media
from ..speech_jobs import latest_speech, published_speech_uses_job
from .queries import session_select

_TIME_RANGE = re.compile(r"(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})")
_SECRET = re.compile(
    r"(?i)(\b(?:authorization|cookie|token|client_secret|api[-_ ]?key|sign_api_key)\s*[:=]\s*)([^\r\n]+)"
)
_URL = re.compile(r"https?://[^\s]+")
_STATES = {
    "stream": {
        "live": ("配信中", "live"),
        "ended": ("終了", "neutral"),
        "offline": ("オフライン", "neutral"),
        "interrupted": ("中断", "warning"),
        "restricted": ("視聴制限", "warning"),
    },
    "recording": {
        "running": ("録画中", "recording"),
        "completed": ("保存済み", "live"),
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
        "not_started": ("未収集", "neutral"),
        "expired": ("期限削除済み", "neutral"),
    },
    "subtitle": {
        "completed": ("保存済み", "live"),
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
        session_select + " WHERE stream_state = 'live' ORDER BY started_at DESC, stream_sessions.id DESC LIMIT 25"
    ).fetchall()
    return {"counts": counts, "running": running, "now": datetime.now(UTC).isoformat()}


def disk_summary(workspace: FixtureWorkspace) -> tuple[int | None, int | None]:
    try:
        volume = os.statvfs(workspace.recordings)
        return volume.f_bavail * volume.f_frsize, volume.f_blocks * volume.f_frsize
    except OSError:
        return None, None


def verified_outputs(database: sqlite3.Connection, workspace: FixtureWorkspace, session_id: int) -> list[dict]:
    segments = database.execute(
        "SELECT * FROM recording_segments WHERE session_id = ? ORDER BY number", (session_id,)
    ).fetchall()
    expiration = database.execute(
        "SELECT state FROM comment_expirations WHERE session_id = ?", (session_id,)
    ).fetchone()
    results = []
    for segment in segments:
        available = matches_media(workspace, segment["video_path"], segment["video_generation"])
        publications = database.execute(
            "SELECT * FROM subtitle_publications WHERE segment_id = ?", (segment["id"],)
        ).fetchall()
        statuses = {}
        speech_origin = None
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
        if expiration:
            statuses.update(
                dict.fromkeys(("comment", "ass"), "expired" if expiration["state"] == "expired" else "blocked")
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
            }
        )
    return results


def speech_cues(
    database: sqlite3.Connection,
    workspace: FixtureWorkspace,
    session_id: int | None,
    term: str,
    page: int,
    order: str,
) -> tuple[list[dict], bool, bool]:
    order_sql = "ASC" if order == "asc" else "DESC"
    rows = database.execute(
        f"""
        SELECT subtitle_publications.*, segment.video_path, segment.video_generation, segment.number, session.title,
               session.id AS session_id
        FROM subtitle_publications
        JOIN recording_segments segment ON segment.id = subtitle_publications.segment_id
        JOIN stream_sessions session ON session.id = segment.session_id
        WHERE kind = 'speech' AND (? IS NULL OR session.id = ?)
        ORDER BY session.started_at {order_sql}, session.id {order_sql}, segment.number {order_sql}
    """,
        (session_id, session_id),
    )
    results = []
    unavailable = False
    matched = 0
    start = (page - 1) * 25
    for row in rows:
        if not (
            matches_media(workspace, row["video_path"], row["video_generation"])
            and matches_media(workspace, row["relative_path"], row["file_generation"])
        ):
            unavailable = True
            continue
        try:
            fd = os.open(workspace.root / row["relative_path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def fixture_events(workspace: FixtureWorkspace) -> tuple[list[dict], str]:
    FixtureWorkspace.open(workspace.root)
    try:
        fd = os.open(workspace.events, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return [], "empty"
    except OSError:
        return [], "unavailable"
    with os.fdopen(fd, "rb") as source:
        size = source.seek(0, os.SEEK_END)
        skipped = max(0, size - 512_000)
        source.seek(skipped)
        records = source.read(512_000).splitlines()
        if skipped:
            records = records[1:]
    results = []
    state = "partial" if skipped else "available"
    for line in reversed(records):
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
        item["detail"] = _URL.sub(
            "［URL省略］",
            _SECRET.sub(
                r"\1［秘匿］",
                str(exception.get("message", "") if isinstance(exception, dict) else entry.get("message", "")),
            ),
        )
        item = {key: (_SECRET.sub(r"\1［秘匿］", value) if key != "detail" else value) for key, value in item.items()}
        results.append(item)
    return results, state
