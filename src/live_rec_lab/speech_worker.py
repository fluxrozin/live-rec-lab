"""Run audio extraction and offline inference in an owned child process."""

import json
import os
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import asdict
from importlib.machinery import PathFinder
from pathlib import Path
from threading import Event
from typing import Literal, cast

from .isolation import Workspace
from .media import generation, media_tool
from .speech import SpeechBlocked, SpeechCue, recognize_local


class AudioUnavailable(ValueError):
    """A verified video has no audio stream."""


class SpeechInterrupted(Exception):
    """The manager stopped the owned inference process."""


class SpeechUnavailable(SpeechBlocked):
    """An offline inference prerequisite is missing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _model(workspace: Workspace, model_name: str) -> Path:
    models = workspace.database.parent / "models"
    candidate = models / model_name
    try:
        workspace.check_directory(models, private=True)
        workspace.check_directory(candidate, private=True)
        for required in ("model.bin", "config.json", "tokenizer.json"):
            generation(candidate / required)
        for entry in candidate.iterdir():
            if entry.is_symlink():
                raise ValueError("model links are not accepted")
    except (OSError, ValueError) as error:
        raise SpeechUnavailable("model_unavailable") from error
    return candidate


def _child_result(result_file: Path) -> list[SpeechCue]:
    with result_file.open(encoding="utf-8") as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("oversized speech result")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("invalid speech result")
    reason = result.get("reason")
    if reason == "no_audio":
        raise AudioUnavailable("no audio stream")
    if reason in {"model_unavailable", "inference_unavailable"}:
        raise SpeechUnavailable(reason)
    if not isinstance(result.get("cues"), list):
        raise ValueError("invalid speech cues")
    cues = []
    for cue in result["cues"]:
        if not (
            isinstance(cue, dict)
            and type(cue.get("start_ms")) is int
            and type(cue.get("end_ms")) is int
            and isinstance(cue.get("text"), str)
        ):
            raise ValueError("invalid speech cue fields")
        cues.append(SpeechCue(cue["start_ms"], cue["end_ms"], cue["text"]))
    return cues


def _cuda_library_dirs() -> str:
    # Search this interpreter's installed wheel locations without importing NVIDIA modules.
    installed = list(dict.fromkeys(sysconfig.get_path(key) for key in ("purelib", "platlib")))
    namespace = PathFinder.find_spec("nvidia", installed)
    parent_paths = namespace.submodule_search_locations if namespace else None
    if parent_paths is None:
        return ""
    return os.pathsep.join(
        str(directory)
        for package in ("cublas", "cudnn")
        for location in parent_paths
        if (directory := Path(location) / package / "lib").is_dir()
    )


def run_local_worker(
    workspace: Workspace,
    video: Path,
    *,
    model_name: str,
    language: str,
    device: str,
    worker_lock: int,
    stop: Event | None = None,
) -> list[SpeechCue]:
    if stop is not None and stop.is_set():
        raise SpeechInterrupted
    try:
        model = str(_model(workspace, model_name))
    except SpeechUnavailable:
        model = ""
    with tempfile.TemporaryDirectory(prefix="speech-", dir=workspace.events.parent) as scratch:
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": scratch,
            "XDG_CACHE_HOME": scratch,
        }
        if device == "cuda" and (paths := _cuda_library_dirs()):
            env["LD_LIBRARY_PATH"] = paths
        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "live_rec_lab.speech_worker",
                str(video),
                model,
                scratch,
                language,
                device,
            ],
            cwd=scratch,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(worker_lock,),
        ) as child:
            try:
                if _wait_child(child, stop, timeout=1800):
                    raise ValueError("speech child failed")
            except BaseException:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                raise
        return _child_result(Path(scratch) / "result.json")


def _wait_child(child: subprocess.Popen, stop: Event | None, *, timeout: float) -> int:
    if stop is None:
        return child.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout)
        try:
            return child.wait(timeout=min(remaining, 0.2))
        except subprocess.TimeoutExpired:
            continue
    raise SpeechInterrupted


def has_audio(video: Path) -> bool:
    audio_streams = media_tool(
        "ffprobe",
        [
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video),
        ],
    )
    return bool(audio_streams.strip())


def extract_audio(video: Path, output: Path, *, probe: bool = True) -> None:
    if probe and not has_audio(video):
        raise AudioUnavailable("no audio stream")
    media_tool(
        "ffmpeg",
        [
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-protocol_whitelist",
            "file",
            "-copyts",
            "-start_at_zero",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-af",
            "aresample=16000:async=1:first_pts=0",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout=300,
    )


def _deny_network(event: str, args: tuple) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        raise PermissionError("external access is disabled in the speech worker")


def main() -> int:
    # This module is executed in an isolated, app-owned scratch directory.
    sys.addaudithook(_deny_network)
    video, model, scratch, language, device = sys.argv[1:]
    work = Path(scratch)
    signal.alarm(1800)
    try:
        if not has_audio(Path(video)):
            raise AudioUnavailable("no audio stream")
        if not model:
            with (work / "result.json").open("x", encoding="utf-8") as result_file:
                json.dump({"reason": "model_unavailable"}, result_file)
            return 0
        extract_audio(Path(video), work / "audio.wav", probe=False)
        cues = recognize_local(
            work / "audio.wav",
            Path(model),
            language=language,
            device=cast(Literal["cuda", "cpu"], device),
        )
        result = {"cues": [asdict(cue) for cue in cues]}
    except AudioUnavailable:
        result = {"reason": "no_audio"}
    except SpeechBlocked:
        result = {"reason": "inference_unavailable"}
    except (OSError, ValueError, subprocess.SubprocessError):
        result = {"reason": "media_or_inference_error"}
    except RuntimeError:
        result = {"reason": "inference_error"}
    with (work / "result.json").open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, allow_nan=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
