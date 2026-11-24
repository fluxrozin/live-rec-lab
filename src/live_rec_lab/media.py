"""Local media generation and exclusive publication."""

import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def _safe_component(value: str) -> str:
    cleaned = _WINDOWS_INVALID.sub("_", unicodedata.normalize("NFC", value)).strip().rstrip(". ")
    if _WINDOWS_RESERVED.match(cleaned):
        return "_" + cleaned
    return cleaned


def automatic_stem(
    platform: str, streamer: str, started: datetime, title: str, number: int, *, collision_id: str | None = None
) -> str:
    if (
        platform not in {"tiktok", "twitcasting"}
        or number < 1
        or collision_id is not None
        and re.fullmatch(r"[0-9a-f]{10}", collision_id) is None
    ):
        raise ValueError("invalid recording name metadata")
    safe_streamer = _safe_component(streamer)
    if not safe_streamer:
        raise ValueError("streamer ID cannot produce an empty filename")
    safe_title = _safe_component(title) or "untitled"
    prefix = f"{platform}_{safe_streamer}_{started:%Y%m%d_%H%M%S}_"
    suffix = (f"_{collision_id}" if collision_id is not None else "") + f"_s{number:03}"
    budget = 240 - len((prefix + suffix + "_comment.srt").encode())
    if budget < len("untitled"):
        raise ValueError("streamer ID exceeds the recording name budget")
    short_title = safe_title.encode()[:budget].decode(errors="ignore").rstrip(". ") or "untitled"
    return prefix + short_title + suffix


def publish_exclusive(temporary: Path, destination: Path) -> None:
    if temporary.parent != destination.parent:
        raise ValueError("publication must stay in the staging directory")
    name = unicodedata.normalize("NFC", destination.name).casefold()
    for existing in destination.parent.iterdir():
        if unicodedata.normalize("NFC", existing.name).casefold() == name:
            raise FileExistsError(f"media name is already occupied: {destination.name}")
    os.link(temporary, destination, follow_symlinks=False)
    temporary.unlink()


def generation(path: Path) -> str:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != os.geteuid():
        raise ValueError("media identity cannot be verified")
    return json.dumps([status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns])


def media_tool(tool: str, arguments: list[str], *, timeout: int = 30) -> str:
    # The synthetic lane needs only these installed binaries and the environment below.
    if tool not in {"ffmpeg", "ffprobe"}:
        raise ValueError("unknown fixture media tool")
    result = subprocess.run(
        [f"/usr/bin/{tool}", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )
    if result.returncode:
        raise ValueError(f"{tool} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _make_fixture_hls(playlist: Path) -> None:
    media_tool(
        "ffmpeg",
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000,adelay=250:all=1",
            "-c:a",
            "aac",
            "-t",
            "2",
            "-threads",
            "1",
            "-c:v",
            "libx264",
            "-g",
            "24",
            "-sc_threshold",
            "0",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            str(playlist),
        ],
    )


def record_fixture_video(destination: Path) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".fixture-hls-") as hls:
        playlist = Path(hls) / "fixture.m3u8"
        _make_fixture_hls(playlist)
        temporary, duration_ms = stage_local_hls(playlist, destination, duration_limit=2)
    try:
        publish_exclusive(temporary, destination)
        return duration_ms, generation(destination)
    finally:
        temporary.unlink(missing_ok=True)


def stage_local_hls(playlist: Path, destination: Path, *, duration_limit: int | None = None) -> tuple[Path, int]:
    """Remux a validated local HLS input to staging in the destination's directory."""
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part.mp4", delete=False) as staging:
        temporary = Path(staging.name)
    duration_arguments = ["-t", str(duration_limit)] if duration_limit is not None else []
    try:
        media_tool(
            "ffmpeg",
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file",
                "-i",
                str(playlist),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                *duration_arguments,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
        )
        metadata = json.loads(
            media_tool(
                "ffprobe",
                [
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-show_entries",
                    "format=duration:stream=codec_type",
                    "-of",
                    "json",
                    str(temporary),
                ],
            )
        )
        duration_ms = round(float(metadata["format"]["duration"]) * 1000)
        if duration_ms <= 0 or not any(stream["codec_type"] == "video" for stream in metadata["streams"]):
            raise ValueError("recording contains no valid video")
        with temporary.open("rb") as media:
            os.fsync(media.fileno())
        return temporary, duration_ms
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        temporary.unlink(missing_ok=True)
        raise


def fixture_dimensions(video: Path, *, timeout: float = 30) -> tuple[int, int]:
    metadata = json.loads(
        media_tool(
            "ffprobe",
            [
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
                "-of",
                "json",
                str(video),
            ],
            timeout=timeout,
        )
    )
    try:
        stream = metadata["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        if min(width, height) <= 0:
            raise ValueError("video dimensions could not be verified")
        rotation = next(
            (entry["rotation"] for entry in stream.get("side_data_list", []) if "rotation" in entry),
            stream.get("tags", {}).get("rotate", 0),
        )
        return (height, width) if abs(float(rotation)) % 180 == 90 else (width, height)
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("video dimensions could not be verified") from error
