"""ツイキャス公式HLSのプリロール判定（要求§4.2.3〜4.2.4、設計§13.3）。

実測（2026-11-25, 公式HLS）: 配信開始直後の先頭に、1280x720・30fpsの黒地に「…」が出る約4秒の映像（無音AAC）が
入る。60フレームの周期を2回繰り返す。本編（704x396等）とは初期化情報・解像度が異なる。
判定は「長さだけ」「不連続だけ」では行わず、デコード後のフレーム内容（既知の1周期との照合）と音声の無音を組み合わせる。
先頭の小さな候補（既定15秒以内）が既知の周期の整数回の繰り返しだけで構成される場合にのみ、正式保存前に除外する。
判別できない候補は本編として保全する。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_DATA = json.loads((Path(__file__).with_name("twitcasting_preroll.json")).read_text("utf-8"))
CYCLE: list[str] = _DATA["frame_md5"]
DIMS = (_DATA["width"], _DATA["height"])
MAX_CANDIDATE_S = 15.0


@dataclass
class HeadVerdict:
    is_preroll: bool
    reason: str


def frame_md5s(ffmpeg: str, path: Path | str, *, limit_s: float | None = None) -> list[str]:
    """先頭から（任意でlimit_s秒まで）のデコード後フレームのmd5列（コンテナ非依存）。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if limit_s:
        cmd += ["-t", str(limit_s)]
    cmd += ["-map", "0:v:0", "-f", "framehash", "-hash", "md5", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return [ln.rsplit(",", 1)[-1].strip() for ln in r.stdout.splitlines() if ln and not ln.startswith("#")]


def audio_is_silent(ffmpeg: str, path: Path | str) -> bool:
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if m is None:
        return True  # 音声なし
    return m.group(1) == "-inf" or float(m.group(1)) <= -80.0


def classify_head(ffmpeg: str, path: Path | str, duration_ms: int | None) -> HeadVerdict:
    """録画ファイル全体が既知のプリロールだけで構成されているか。"""
    if duration_ms is None or duration_ms > MAX_CANDIDATE_S * 1000:
        return HeadVerdict(False, "候補が長く、プリロールだけとは判定できない")
    frames = frame_md5s(ffmpeg, path)
    n = len(CYCLE)
    if not frames or len(frames) % n != 0:
        return HeadVerdict(False, f"フレーム数 {len(frames)} が周期 {n} の整数倍ではない")
    if any(frames[i:i + n] != CYCLE for i in range(0, len(frames), n)):
        return HeadVerdict(False, "既知のプリロール周期と内容が一致しない")
    if not audio_is_silent(ffmpeg, path):
        return HeadVerdict(False, "音声が無音ではない")
    return HeadVerdict(True, f"既知のプリロール周期 × {len(frames) // n} と一致")


def find_main_start(ffmpeg: str, ffprobe: str, path: Path | str) -> float | None:
    """先頭がプリロールで、その後に本編が続く場合の本編開始時刻（秒）。先頭がプリロールでなければNone。

    先頭 MAX_CANDIDATE_S 秒内で、既知の周期の整数回だけ続いたあと最初に周期から外れる位置（キーフレーム）を返す。
    """
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time,key_frame",
                        "-read_intervals", f"%+{MAX_CANDIDATE_S + 5}", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=120)
    frames = []
    for ln in r.stdout.splitlines():
        parts = ln.split(",")
        if len(parts) >= 2:
            try:
                frames.append((float(parts[1]) if parts[0] in ("0", "1") else float(parts[0]), parts[0] == "1" or parts[1] == "1"))
            except ValueError:
                continue
    hashes = frame_md5s(ffmpeg, path, limit_s=MAX_CANDIDATE_S + 5)
    n = len(CYCLE)
    matched = 0
    while (matched + n) <= len(hashes) and hashes[matched:matched + n] == CYCLE:
        matched += n
    if matched == 0 or matched >= len(hashes):
        return None
    # 本編の最初のフレームの時刻（プリロール終端）
    t = _frame_time(ffprobe, path, matched)
    return t


def _frame_time(ffprobe: str, path: Path | str, index: int) -> float | None:
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                        "-of", "csv=p=0", "-read_intervals", f"%+{MAX_CANDIDATE_S + 5}", str(path)],
                       capture_output=True, text=True, timeout=120)
    ts = []
    for ln in r.stdout.splitlines():
        try:
            ts.append(float(ln.split(",")[0]))
        except ValueError:
            continue
    ts.sort()
    # -ss は入力の先頭時刻（コンテナ開始オフセット）からの相対秒なので、先頭フレームの時刻を引く
    return ts[index] - ts[0] if index < len(ts) else None
