"""一時ファイルを検証してから正式名にする。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import RecordingSegment, StreamSession
from live_rec_lab.recording.engines import media_binary
from live_rec_lab.storage import matching_windows_name, publish_exclusive, safe_managed_path, windows_name_key

_FORMATS = {"mpegts": ".ts", "flv": ".flv", "matroska": ".mkv", "mp4": ".mp4"}
_MAX_PROBE_SECONDS = 30
_MAX_REMUX_SECONDS = 900
_MAX_TWITCASTING_PREROLL_BYTES = 512 * 1024
# Packet count and ordered packet-hash digest for one cycle of the verified
# TwitCasting preroll, in video (H.264 slices), audio (AAC payload) order.
_TWITCASTING_PREROLL_BLOCKS = (
    (60, "1811e8279e77daa53520dc6f5ec855d74e32b0c825441bb8260a36c22b6e5f68"),
    (95, "d3c422f408a0672b7eb17a95049cb8c3f0fb6beee3725898fe84551150787dbc"),
)


def is_twitcasting_preroll(path: Path) -> bool:
    """長さやファイル名に頼らず、映像・音声が両方一致する既知の素材を判定する。"""
    try:
        if not 0 < _stored_size(path) <= _MAX_TWITCASTING_PREROLL_BYTES:
            return False
        binary = media_binary("ffmpeg")
        if binary is None:
            return False
        result = subprocess.run(
            [
                binary, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file",
                "-f", "mpegts", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                "-bsf:v", "h264_mp4toannexb,filter_units=pass_types=1-5", "-bsf:a", "aac_adtstoasc",
                "-f", "framehash", "-hash", "sha256", "pipe:1",
            ],
            capture_output=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False

    packets: dict[bytes, list[bytes]] = {b"0": [], b"1": []}
    for line in result.stdout.splitlines():
        if not line or line.startswith(b"#"):
            continue
        fields = line.split(b",", 6)
        if len(fields) < 6 or fields[0].strip() not in packets or len(fields[5].strip()) != 64:
            return False
        packets[fields[0].strip()].append(fields[5].strip())

    loops = set()
    for stream, (count, expected) in zip(packets.values(), _TWITCASTING_PREROLL_BLOCKS, strict=True):
        repetitions, extra = divmod(len(stream), count)
        if not repetitions or extra:
            return False
        if any(
            hashlib.sha256(b"".join(stream[start:start + count])).hexdigest() != expected
            for start in range(0, len(stream), count)
        ):
            return False
        loops.add(repetitions)
    return len(loops) == 1


@dataclass
class Probe:
    duration_ms: int
    format_name: str
    start_pts: float | None
    video_codec: str | None
    unnamed_video: bool = False
    audio_codec: str | None = None

    @property
    def validation_error(self) -> str | None:
        if not self.video_codec or self.unnamed_video:
            return "映像コーデックを読めません"
        if not self.audio_codec:
            return "音声コーデックを読めません"
        if self.duration_ms <= 0:
            return "録画時間を確認できません"
        return None


def describe_probe_failure(stderr: str, size: int) -> str:
    """空のファイルと、コーデックを読めなかったファイルを区別する。"""
    if size <= 0:
        return "録画ファイルが空です"
    if "codec" in stderr.lower():
        return "映像コーデックを読めません"
    return "録画ファイルを検証できません"


def _video_streams(payload: dict) -> list[dict]:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return []
    return [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]


def _stream_codec(payload: dict, media_type: str) -> str | None:
    for item in payload.get("streams", []):
        if not isinstance(item, dict) or item.get("codec_type") != media_type:
            continue
        name = item.get("codec_name")
        if isinstance(name, str) and name.lower() not in ("unknown", "none", ""):
            return name
    return None


def _unnamed_video(payload: dict) -> bool:
    """映像ストリームがあるのに、名前を読めないときだけ真。音声だけは偽。"""
    for item in _video_streams(payload):
        name = item.get("codec_name")
        if not isinstance(name, str) or not name:
            return True
    return False


def probe_media(path: Path) -> Probe | None:
    binary = media_binary("ffprobe")
    if binary is None or not _stored_size(path):
        return None
    try:
        completed = subprocess.run(
            [binary, "-v", "error", "-protocol_whitelist", "file,crypto,data", "-show_entries",
             "format=duration,format_name,start_time:stream=codec_name,codec_type", "-of", "json", str(path)],
            capture_output=True,
            check=False,
            timeout=_MAX_PROBE_SECONDS,
        )
        if completed.returncode != 0:
            return None
        payload = json.loads(completed.stdout.decode("utf-8"))
        info = payload["format"]
        duration = float(info["duration"])
        start = info.get("start_time")
        start_pts = float(start) if start not in (None, "N/A") else None
    except (KeyError, ValueError, TypeError, OSError, subprocess.TimeoutExpired):
        return None
    if not math.isfinite(duration) or duration < 0.001:
        return None
    if start_pts is not None and not math.isfinite(start_pts):
        start_pts = None
    unnamed = _unnamed_video(payload)
    codec = None if unnamed else _stream_codec(payload, "video")
    return Probe(int(duration * 1000), str(info.get("format_name") or ""), start_pts, codec, unnamed,
                 _stream_codec(payload, "audio"))


def remux_args(ffmpeg: str, source: Path, dest: Path, video_codec: str | None) -> list[str]:
    args = [
        ffmpeg,
        "-nostdin",
        "-n",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,crypto,data",
        "-i",
        str(source),
        "-c",
        "copy",
    ]
    if video_codec == "hevc":
        args.extend(["-tag:v", "hvc1"])
    args.extend(["-movflags", "+faststart", str(dest)])
    return args


def remux_mp4(source: Path, dest: Path, video_codec: str | None = None) -> bool:
    binary = media_binary("ffmpeg")
    if binary is None:
        return False
    try:
        completed = subprocess.run(
            remux_args(binary, source, dest, video_codec),
            capture_output=True,
            check=False,
            timeout=_MAX_REMUX_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and _stored_size(dest) > 0


def reserve_segment_path(db: Session, segment: RecordingSegment, root: Path) -> None:
    """呼び出し側で BEGIN IMMEDIATE。ファイルを発行する前にこの更新をコミットする。"""
    if not segment.temp_path or not _stored_size(safe_managed_path(root, Path(segment.temp_path))):
        return
    if segment.reserved_path:
        return
    stream = db.get(StreamSession, segment.stream_session_id)
    if stream is None or not stream.filename_stem:
        raise ValueError("配信セッションのファイル名を確認できません")
    stem = Path(stream.filename_stem).name
    directory = Path(segment.temp_path).parent
    highest = db.scalar(select(func.max(RecordingSegment.segment_number)).where(
        RecordingSegment.stream_session_id == stream.id
    )) or 0
    number = segment.segment_number or max(stream.next_segment_number, highest + 1)
    occupied = _occupied_paths(db, directory, segment.id)
    disambiguator = stream.filename_disambiguator or ""
    dest = directory / f"{stem}{disambiguator}_s{number:03d}.mp4"
    if number == 1 and highest == 0 and not disambiguator and _name_conflicts(dest, occupied):
        token = hashlib.sha256(str(stream.id).encode()).hexdigest()
        for length in range(8, 39, 4):
            disambiguator = "_q" + token[:length]
            dest = directory / f"{stem}{disambiguator}_s{number:03d}.mp4"
            if not _name_conflicts(dest, occupied):
                stream.filename_disambiguator = disambiguator
                break
        else:
            raise FileExistsError("配信セッションの正式名を予約できません")
    segment.segment_number = number
    if _file_stem_key(dest.name) in occupied:
        raise FileExistsError("正式名は別のセグメントが予約しています")
    segment.reserved_path = str(safe_managed_path(root, dest))
    stream.next_segment_number = max(stream.next_segment_number, number + 1)


def _occupied_paths(db: Session, directory: Path, segment_id: int) -> set[str]:
    paths = db.execute(select(RecordingSegment.reserved_path, RecordingSegment.file_path).where(
        RecordingSegment.id != segment_id,
        or_(RecordingSegment.reserved_path.startswith(str(directory) + "/", autoescape=True),
            RecordingSegment.file_path.startswith(str(directory) + "/", autoescape=True)),
    ))
    return {_file_stem_key(Path(path).name) for row in paths for path in row if path}


def _name_conflicts(path: Path, occupied: set[str]) -> bool:
    stem = _file_stem_key(path.name)
    return stem in occupied or _directory_contains_stem(path.parent, stem)


def _directory_contains_stem(directory: Path, stem: str) -> bool:
    with os.scandir(directory) as entries:
        return any(_file_stem_key(entry.name) == stem for entry in entries)


def _file_stem_key(name: str) -> str:
    return Path(windows_name_key(name)).stem


def attempt_is_empty(segment: RecordingSegment) -> bool:
    """バイトが残っていない失敗だけ真。有効なファイルと、中身のある失敗は偽。"""
    if segment.validation_status == "valid":
        return False
    return _stored_path_empty(segment.temp_path) and _stored_path_empty(segment.file_path)


def _stored_path_empty(value: str | None) -> bool:
    return not value or not _stored_size(Path(value))


def unlink_empty_files(segment: RecordingSegment) -> None:
    if segment.temp_path:
        path = Path(segment.temp_path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISREG(info.st_mode) and info.st_size == 0:
            path.unlink()


def _stored_size(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def finalize_segment(segment: RecordingSegment, *, retain_staging: bool = False) -> str | None:
    segment.finished_at = segment.finished_at or utcnow()
    temp = Path(segment.temp_path) if segment.temp_path else None
    if temp is None or not _stored_size(temp):
        segment.validation_status = "invalid"
        return describe_probe_failure("", 0)
    reserved = segment.reserved_path or segment.file_path
    if not reserved:
        return "録画ファイルの正式名が未予約です"
    dest = Path(reserved)
    if dest.parent != temp.parent:
        return "仮ファイルと正式名が同じフォルダにありません"
    staged = temp.with_name(temp.name + ".remux.mp4")
    own_output = _existing_output(dest, temp, staged)
    if own_output:
        return _confirm_file(segment, own_output, retain_staging=retain_staging)
    if _directory_contains_stem(dest.parent, _file_stem_key(dest.name)):
        return "録画ファイルの正式名が既存ファイルと衝突しています"
    info = probe_media(temp)
    segment.file_size = _stored_size(temp)
    error = info.validation_error if info else "録画ファイルを検証できません"
    if error:
        segment.validation_status = "invalid"
        return error
    assert info is not None
    source = temp
    suffix = _format_suffix(info.format_name)
    if suffix != ".mp4" and matching_windows_name(staged) is None and remux_mp4(temp, staged, info.video_codec):
        converted = probe_media(staged)
        if converted and not converted.validation_error and _format_suffix(converted.format_name) == ".mp4":
            source, suffix = staged, ".mp4"
    if not suffix:
        segment.validation_status = "invalid"
        return "録画形式を確認できません"
    final = dest.with_suffix(suffix)
    try:
        if _directory_contains_stem(final.parent, _file_stem_key(final.name)):
            raise FileExistsError(final)
        publish_exclusive(source, final)
    except (OSError, ValueError) as exc:
        return f"録画ファイルを上書きせずに確定できません: {exc}"
    return _confirm_file(segment, final, retain_staging=retain_staging)


def _format_suffix(formats: str) -> str | None:
    return next((_FORMATS[item] for item in formats.split(",") if item in _FORMATS), None)


def _existing_output(dest: Path, temp: Path, staged: Path) -> Path | None:
    for suffix in _FORMATS.values():
        candidate = dest.with_suffix(suffix)
        if matching_windows_name(candidate) != candidate or not _stored_size(candidate):
            continue
        for source in (temp, staged):
            if _stored_size(source) and source.samefile(candidate):
                return candidate
    return None


def _confirm_file(segment: RecordingSegment, final: Path, *, retain_staging: bool) -> str | None:
    info = probe_media(final)
    if info is None or info.validation_error or _format_suffix(info.format_name) != final.suffix:
        segment.validation_status = "invalid"
        return "公開後の録画ファイルを検証できません"
    identity = final.lstat()
    segment.file_path = str(final)
    segment.file_device = identity.st_dev
    segment.file_inode = identity.st_ino
    segment.file_size = identity.st_size
    segment.duration_ms = info.duration_ms
    segment.media_start_pts = info.start_pts
    segment.container_format = next(key for key, suffix in _FORMATS.items() if suffix == final.suffix)
    segment.validation_status = "valid"
    if segment.sync_method is None:
        segment.sync_method = "approximate"
    if not retain_staging:
        clean_published_staging(segment)
    return None


def clean_published_staging(segment: RecordingSegment) -> None:
    if not segment.temp_path or segment.validation_status != "valid" or not segment.file_path:
        return
    temporary = Path(segment.temp_path)
    final = Path(segment.file_path)
    if temporary.parent != final.parent or temporary == final:
        return
    for path in (temporary, temporary.with_name(temporary.name + ".remux.mp4")):
        if _stored_size(path):
            path.unlink()
    segment.temp_path = None
