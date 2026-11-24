"""Prepare and publish bounded media inputs behind the provider collection boundary."""

import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..collection import _media_directory
from ..db import connect
from ..media import _safe_component, automatic_stem, generation, publish_exclusive, stage_local_hls
from ..retention import deleted_source, next_segment_id

if TYPE_CHECKING:
    from ..isolation import Workspace

_RECORDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_media_folders (
    normalized_id TEXT PRIMARY KEY,
    folder TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS provider_recording_attempts (
    session_id INTEGER NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
    input_key TEXT NOT NULL,
    intended_path TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (session_id, input_key)
);
CREATE TABLE IF NOT EXISTS provider_recording_segments (
    segment_id INTEGER PRIMARY KEY REFERENCES recording_segments(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
    input_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completes_session INTEGER NOT NULL CHECK (completes_session IN (0, 1)),
    UNIQUE (session_id, input_key)
);
CREATE TABLE IF NOT EXISTS provider_recording_failures (
    session_id INTEGER PRIMARY KEY REFERENCES stream_sessions(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL CHECK (attempts > 0)
);
CREATE TABLE IF NOT EXISTS provider_recording_names (
    session_id INTEGER PRIMARY KEY REFERENCES stream_sessions(id) ON DELETE CASCADE,
    date_folder TEXT NOT NULL,
    filename_prefix TEXT NOT NULL
);
"""
_NAME_RESERVATION_NUMBER = 999_999
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,100}\.ts\Z")
_HLS_TAGS = {
    "#EXTM3U",
    "#EXT-X-VERSION",
    "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE",
    "#EXT-X-DISCONTINUITY",
    "#EXT-X-DISCONTINUITY-SEQUENCE",
    "#EXTINF",
    "#EXT-X-ENDLIST",
    "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-INDEPENDENT-SEGMENTS",
}
_MAX_SOURCE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class RecordingInput:
    input_key: str
    playlist: Path
    started_at: datetime
    service: str
    account: str
    live_id: str
    complete: bool = False


@dataclass(frozen=True)
class RecordingDestination:
    number: int
    path: Path
    directory_identity: tuple[int, int]


def _directory_identity(path: Path) -> tuple[int, int]:
    details = path.lstat()
    return details.st_dev, details.st_ino


@contextmanager
def _sealed_playlist(workspace: "Workspace", playlist: Path):
    """Copy an owned, already downloaded transport-stream playlist into a self-contained input."""
    base = workspace.database.parent / "recording-inputs"
    if (
        playlist.suffix != ".m3u8"
        or not playlist.is_relative_to(base)
        or playlist.resolve(strict=True) != playlist
        or playlist.stat().st_size > 65536
    ):
        raise ValueError("recording input is outside its staging area")
    before = generation(playlist)
    lines = playlist.read_text(encoding="utf-8").splitlines()
    if generation(playlist) != before:
        raise ValueError("recording playlist changed")
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        raise ValueError("recording playlist is not finished")
    with tempfile.TemporaryDirectory(dir=base, prefix=".sealed-") as directory:
        sealed = Path(directory) / "playlist.m3u8"
        total = 0
        names: set[str] = set()
        for line in lines:
            if line.startswith("#"):
                if line.partition(":")[0] not in _HLS_TAGS:
                    raise ValueError("unsupported recording playlist tag")
                continue
            if not _SEGMENT.fullmatch(line) or line in names or len(names) >= 256:
                raise ValueError("recording playlist references unverified media")
            media = playlist.with_name(line)
            media_generation = generation(media)
            total += media.stat().st_size
            if total > _MAX_SOURCE_BYTES:
                raise ValueError("recording playlist exceeds its staging budget")
            shutil.copyfile(media, sealed.with_name(line), follow_symlinks=False)
            if generation(media) != media_generation:
                raise ValueError("recording transport segment changed")
            names.add(line)
        if not names:
            raise ValueError("recording playlist contains no media")
        sealed.write_text("\n".join(lines) + "\n", encoding="utf-8")
        yield sealed


def recording_schema(database: sqlite3.Connection) -> None:
    database.executescript(_RECORDING_SCHEMA)


def _folder(database: sqlite3.Connection, account: str) -> str:
    existing = database.execute(
        "SELECT folder FROM provider_media_folders WHERE normalized_id = ?", (account,)
    ).fetchone()
    if existing:
        return existing["folder"]
    folder = _safe_component(account)
    if database.execute("SELECT 1 FROM provider_media_folders WHERE folder = ?", (folder,)).fetchone():
        folder = f"{folder}-{sha256(account.encode()).hexdigest()[:12]}"
    database.execute("INSERT INTO provider_media_folders VALUES (?, ?)", (account, folder))
    return folder


def _start_in_recording_timezone(database: sqlite3.Connection, started_at: str) -> datetime:
    timezone = "Asia/Tokyo"
    if database.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'fixture_display_preferences' AND type = 'table'"
    ).fetchone():
        preference = database.execute(
            "SELECT display_timezone FROM fixture_display_preferences WHERE id = 1"
        ).fetchone()
        if preference:
            timezone = preference["display_timezone"]
    return datetime.fromisoformat(started_at).astimezone(ZoneInfo(timezone))


def _filename_prefix(session: sqlite3.Row, started: datetime, collision_id: str | None = None) -> str:
    return automatic_stem(
        session["platform"],
        session["normalized_id"],
        started,
        session["title"],
        _NAME_RESERVATION_NUMBER,
        collision_id=collision_id,
    ).removesuffix(f"_s{_NAME_RESERVATION_NUMBER}")


def _media_name_occupied(video: Path) -> bool:
    target_names = {
        video.name.casefold(),
        f"{video.stem.casefold()}_comment.srt",
        f"{video.stem.casefold()}_speech.srt",
        f"{video.stem.casefold()}.ass",
    }
    return any(entry.name.casefold() in target_names for entry in video.parent.iterdir())


def _destination(
    database: sqlite3.Connection, workspace: "Workspace", session: sqlite3.Row, source: RecordingInput
) -> RecordingDestination | None:
    if deleted_source(database, session["streamer_id"], session["source_id"]):
        return None
    prior = database.execute(
        "SELECT state FROM provider_recording_attempts WHERE session_id = ? AND input_key = ?",
        (session["id"], source.input_key),
    ).fetchone()
    if prior and prior["state"] != "failed":
        return None
    number = database.execute(
        "SELECT coalesce(max(number), 0) + 1 FROM recording_segments WHERE session_id = ?", (session["id"],)
    ).fetchone()[0]
    folder = _folder(database, session["normalized_id"])
    account_root = _media_directory(database, workspace, workspace.recordings / folder)
    fixed = database.execute(
        "SELECT date_folder, filename_prefix FROM provider_recording_names WHERE session_id = ?",
        (session["id"],),
    ).fetchone()
    suffix = f"_s{number:03}.mp4"
    if len(str(number)) > len(str(_NAME_RESERVATION_NUMBER)):
        raise ValueError("recording filename reservation cannot hold this segment number")
    if fixed:
        if (
            not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", fixed["date_folder"])
            or Path(fixed["filename_prefix"]).name != fixed["filename_prefix"]
        ):
            raise ValueError("recording filename reservation is unsafe")
        directory = _media_directory(database, workspace, account_root / fixed["date_folder"])
        video = directory / (fixed["filename_prefix"] + suffix)
        if _media_name_occupied(video):
            raise FileExistsError("the reserved recording name is already occupied")
        return RecordingDestination(number, video, _directory_identity(directory))
    started = _start_in_recording_timezone(database, session["started_at"])
    date_folder = started.date().isoformat()
    directory = _media_directory(database, workspace, account_root / date_folder)
    prefix = _filename_prefix(session, started)
    video = directory / (prefix + suffix)
    if _media_name_occupied(video):
        collision_id = sha256(
            f"{session['platform']}:{session['normalized_id']}:{session['source_id']}".encode()
        ).hexdigest()[:10]
        prefix = _filename_prefix(session, started, collision_id)
        video = directory / (prefix + suffix)
        if _media_name_occupied(video):
            raise FileExistsError("the disambiguated recording name is already occupied")
    with database:
        database.execute("INSERT INTO provider_recording_names VALUES (?, ?, ?)", (session["id"], date_folder, prefix))
    return RecordingDestination(number, video, _directory_identity(directory))


def save_recording(workspace: "Workspace", session: sqlite3.Row, source: RecordingInput) -> None:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", source.input_key)
        or source.started_at.utcoffset() is None
        or (source.service, source.account, source.live_id)
        != (session["platform"], session["normalized_id"], session["source_id"])
    ):
        raise ValueError("recording input identity or timeline is invalid")
    staged = staged_generation = None
    try:
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            workspace.require_media()
            target = _destination(database, workspace, session, source)
            if target is None:
                return
            relative = str(workspace.media_relative(target.path))
            with database:
                database.execute(
                    """
                    INSERT INTO provider_recording_attempts VALUES (?, ?, ?, 'running')
                    ON CONFLICT(session_id, input_key) DO UPDATE SET
                        intended_path = excluded.intended_path, state = 'running'
                    """,
                    (session["id"], source.input_key, relative),
                )
                database.execute(
                    "UPDATE stream_sessions SET recording_state = 'running' WHERE id = ?", (session["id"],)
                )
        with _sealed_playlist(workspace, source.playlist) as playlist:
            staged, duration = stage_local_hls(playlist, target.path)
            staged_generation = generation(staged)
        with workspace.exclusive(), closing(connect(workspace.database)) as database:
            workspace.require_media()
            if (
                target.path.parent.resolve(strict=True) != target.path.parent
                or _directory_identity(target.path.parent) != target.directory_identity
                or generation(staged) != staged_generation
                or deleted_source(database, session["streamer_id"], session["source_id"])
            ):
                raise ValueError("recording destination changed")
            if _media_name_occupied(target.path):
                raise FileExistsError("the reserved recording name is already occupied")
            publish_exclusive(staged, target.path)
            with database:
                segment_id = next_segment_id(database)
                database.execute(
                    """
                    INSERT INTO recording_segments (id, session_id, number, video_path, video_generation, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (segment_id, session["id"], target.number, relative, generation(target.path), duration),
                )
                database.execute(
                    "INSERT INTO provider_recording_segments VALUES (?, ?, ?, ?, ?)",
                    (
                        segment_id,
                        session["id"],
                        source.input_key,
                        source.started_at.astimezone(UTC).isoformat(),
                        source.complete,
                    ),
                )
                database.execute(
                    "UPDATE provider_recording_attempts SET state = 'completed' WHERE session_id = ? AND input_key = ?",
                    (session["id"], source.input_key),
                )
                database.execute(
                    "UPDATE stream_sessions SET recording_state = 'partial' WHERE id = ?", (session["id"],)
                )
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        review = isinstance(error, (sqlite3.Error, FileExistsError))
        with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
            database.execute(
                "UPDATE provider_recording_attempts SET state = ? WHERE session_id = ? AND input_key = ?",
                ("needs_review" if review else "failed", session["id"], source.input_key),
            )
        if review:
            raise FileExistsError("recording publication needs review") from None
        raise
    finally:
        if staged is not None:
            try:
                if (
                    _directory_identity(staged.parent) == target.directory_identity
                    and generation(staged) == staged_generation
                ):
                    staged.unlink()
            except (OSError, ValueError):
                pass
