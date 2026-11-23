"""録画コマンド。シェルは使わない。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from live_rec_lab.ops.logging_config import redact
from live_rec_lab.platforms.twitcasting import TWITCASTING_REFERER, TWITCASTING_USER_AGENT


def installed_ffmpeg_dir() -> Path | None:
    configured = os.environ.get("LIVE_REC_LAB_FFMPEG_BIN", "").strip()
    return Path(configured).expanduser() if configured else None


def media_binary(name: str) -> str | None:
    folder = installed_ffmpeg_dir()
    if name not in {"ffmpeg", "ffprobe"}:
        return None
    if folder is not None:
        if not folder.is_absolute():
            return None
        pair = (folder / "ffmpeg", folder / "ffprobe")
        if all(path.is_file() and os.access(path, os.X_OK) for path in pair):
            return str(folder / name)
        return None
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe and Path(ffmpeg).resolve().parent == Path(ffprobe).resolve().parent:
        return ffmpeg if name == "ffmpeg" else ffprobe
    return None


@lru_cache(maxsize=8)
def _inspect_media_binary(path: str, mtime_ns: int) -> tuple[str | None, tuple[str, ...]]:
    try:
        version = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=3, check=True)
        decoders = subprocess.run([path, "-decoders"], capture_output=True, text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        return None, ()
    codecs = tuple(
        codec for codec in ("h264", "hevc", "aac")
        if any(line.split()[1:2] == [codec] for line in decoders.stdout.splitlines())
    )
    return next(iter(version.stdout.splitlines()), None), codecs


def media_diagnostics() -> list[dict]:
    tools = []
    for name in ("ffmpeg", "ffprobe"):
        binary = media_binary(name)
        version, codecs = None, ()
        if binary:
            binary = str(Path(binary).resolve())
            try:
                version, codecs = _inspect_media_binary(binary, Path(binary).stat().st_mtime_ns)
            except OSError:
                pass
        tools.append({"name": name, "path": binary, "version": version, "codecs": codecs})
    return tools


def build_command(engine: str, url: str, output: Path, quality: str) -> list[str]:
    if engine == "streamlink":
        binary = shutil.which("streamlink")
        if binary is None:
            raise FileNotFoundError("streamlink")
        stream = "best" if quality != "worst" else "worst"
        return [
            binary,
            "--no-config",
            "--output",
            str(output),
            "--progress",
            "no",
            url,
            stream,
        ]
    if engine == "yt-dlp":
        binary = shutil.which("yt-dlp")
        if binary is None:
            raise FileNotFoundError("yt-dlp")
        return [
            binary,
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--hls-use-mpegts",
            "--no-part",
            "--no-mtime",
            "--no-overwrites",
            "-f",
            "best" if quality != "worst" else "worst",
            "-o",
            str(output),
            url,
        ]
    if engine == "ffmpeg":
        binary = media_binary("ffmpeg")
        if binary is None:
            raise FileNotFoundError("ffmpeg")
        return [
            binary,
            "-nostdin",
            "-n",
            "-loglevel",
            "error",
            "-icy",
            "0",
            "-user_agent",
            TWITCASTING_USER_AGENT,
            "-headers",
            f"Referer: {TWITCASTING_REFERER}\r\n",
            "-i",
            url,
            "-c",
            "copy",
            "-f",
            "mpegts",
            str(output),
        ]
    raise ValueError("未知の録画エンジンです")


async def run_command(
    command: list[str],
    stop: asyncio.Event,
    on_tick=None,
    on_process=None,
) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={key: value for key, value in os.environ.items()
             if key not in {"SIGN_API_KEY", "TWITCASTING_CLIENT_ID", "TWITCASTING_CLIENT_SECRET"}},
    )
    if on_process is not None:
        on_process(process)
    stderr_task = asyncio.create_task(_stderr_tail(process.stderr))
    try:
        stop_hits = 0
        while True:
            if on_tick is not None:
                on_tick()
            if stop.is_set() and process.returncode is None:
                stop_hits += 1
                if stop_hits == 1:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                elif stop_hits >= 10:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            try:
                code = await asyncio.wait_for(process.wait(), timeout=0.5)
                break
            except TimeoutError:
                continue
        error = redact((await stderr_task).decode("utf-8", errors="replace"))
        return code if code is not None else 1, error[:500]
    finally:
        await reap_process(process, stderr_task)


async def reap_process(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[bytes] | None = None,
) -> None:
    """子プロセスを終わらせてから戻る。取消中でも待ち切る。"""
    task = asyncio.current_task()
    pending = process.returncode is None or (stderr_task is not None and not stderr_task.done())
    cancelling = task is not None and task.cancelling() > 0
    if cancelling and pending and task is not None:
        task.uncancel()
    try:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if stderr_task is not None and not stderr_task.done():
            try:
                await stderr_task
            except Exception:
                stderr_task.cancel()
    finally:
        if cancelling and task is not None:
            task.cancel()


async def _stderr_tail(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    tail = b""
    while chunk := await stream.read(8192):
        tail = (tail + chunk)[-16_384:]
    return tail
