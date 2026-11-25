"""ffmpeg/ffprobe の解決と診断（同一ディレクトリの組を検証、要求§2.3）。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MediaTools:
    ffmpeg: str | None
    ffprobe: str | None
    ffmpeg_version: str | None
    ffprobe_version: str | None
    pair_consistent: bool
    decoders: dict[str, bool]
    source: str
    error: str | None = None
    https: bool = False  # このffmpegがHTTPS(TLS)入力を扱えるか（TLSなしビルドは公式HLSを直接読めない）

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe and self.pair_consistent and not self.error)


def _version(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\w+ version (\S+)", out)
    return m.group(1) if m else None


def resolve(bin_dir: str | None = None) -> MediaTools:
    """設定ディレクトリ→起動プロセスのPATH の順で ffmpeg/ffprobe の組を解決する。"""
    if bin_dir:
        ff, fp = (Path(bin_dir) / "ffmpeg"), (Path(bin_dir) / "ffprobe")
        ffs = str(ff) if os.access(ff, os.X_OK) else None
        fps = str(fp) if os.access(fp, os.X_OK) else None
        source = f"LIVE_REC_LAB_FFMPEG_BIN={bin_dir}"
    else:
        ffs, fps = shutil.which("ffmpeg"), shutil.which("ffprobe")
        source = "PATH"
    if not ffs or not fps:
        return MediaTools(ffs, fps, None, None, False, {}, source, "ffmpeg/ffprobeの組を解決できません")
    fv, pv = _version(ffs), _version(fps)
    consistent = fv is not None and fv == pv and Path(ffs).parent == Path(fps).parent
    decoders: dict[str, bool] = {}
    try:
        out = subprocess.run([ffs, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=15).stdout
        for name in ("h264", "hevc", "aac"):
            decoders[name] = bool(re.search(rf"\s{name}\s", out))
    except (OSError, subprocess.SubprocessError):
        pass
    err = None if consistent else "ffmpegとffprobeの版または配置が一致しません"
    https = False
    try:
        out = subprocess.run([ffs, "-hide_banner", "-protocols"], capture_output=True, text=True, timeout=15).stdout
        https = bool(re.search(r"^\s*https\s*$", out, re.M))
    except (OSError, subprocess.SubprocessError):
        pass
    return MediaTools(ffs, fps, fv, pv, consistent, decoders, source, err, https)


@dataclass
class Probe:
    ok: bool
    duration_ms: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    container: str | None = None
    size: int | None = None
    start_time: float | None = None
    error: str | None = None
    width: int | None = None
    height: int | None = None


def probe(ffprobe: str, path: Path | str) -> Probe:
    """ffprobeで形式・映像コーデック・長さ・音声を取得する（存在・サイズ・長さだけで成功にしない）。"""
    import json

    try:
        r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return Probe(False, error=f"ffprobe実行失敗: {exc}")
    if r.returncode != 0:
        return Probe(False, error=(r.stderr or "ffprobe失敗")[-300:])
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return Probe(False, error="ffprobe出力を解析できません")
    fmt = data.get("format", {})
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = fmt.get("duration")
    if dur is None and v is not None:
        dur = v.get("duration")
    try:
        duration_ms = int(float(dur) * 1000) if dur is not None else None
    except ValueError:
        duration_ms = None
    p = Probe(True, duration_ms, (v or {}).get("codec_name"), (a or {}).get("codec_name"), fmt.get("format_name"),
              int(fmt["size"]) if fmt.get("size") else None,
              float(fmt["start_time"]) if fmt.get("start_time") not in (None, "N/A") else None,
              None, (v or {}).get("width"), (v or {}).get("height"))
    if v is None or not v.get("codec_name") or v.get("codec_name") == "unknown":
        p.ok, p.error = False, "映像ストリームまたは映像コーデックを識別できません"
    elif not duration_ms or duration_ms <= 0:
        p.ok, p.error = False, "再生時間を取得できません"
    return p
