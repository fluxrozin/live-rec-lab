"""音声文字起こしの子プロセス（管理プロセスから起動。推論ライブラリはここだけでimportする）。

    python -m live_rec_lab.speech_worker --video V --out OUT.json --model-dir D --model large-v3 --language ja --device cuda

終了コード: 0=成功（OUTにJSON）、2=blocked（reasonを標準出力の1行JSONで返す）、1=失敗。
音声・発話本文を標準出力/ログへ出さない。暗黙にCPUへ切り替えない。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from live_rec_lab.config import apply_hf_token


def emit(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def extract_audio(video: str, wav: str, ffmpeg: str) -> None:
    # 動画0秒を維持（-vn のみ。-ss等で切らない）。16kHzモノラル。
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "wav", wav], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("音声抽出に失敗: " + r.stderr[-200:])


def preload_cuda_libs() -> None:
    """pipで導入したCUDAランタイム(cuBLAS/cuDNN)を先に読み込む（LD_LIBRARY_PATHに依存しない）。"""
    import ctypes
    import importlib.util

    for pkg, names in (("nvidia.cublas", ("libcublasLt.so.12", "libcublas.so.12")),
                       ("nvidia.cudnn", ("libcudnn.so.9",))):
        spec = importlib.util.find_spec(pkg)
        if spec is None or not spec.submodule_search_locations:
            continue
        for base in spec.submodule_search_locations:
            for n in names:
                f = Path(base) / "lib" / n
                if f.exists():
                    try:
                        ctypes.CDLL(str(f), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass


def load_wav(path: str):
    """16kHzモノラルWAV → float32。PyAV経由のデコードに依存しない（faster-whisper 1.2.1 と PyAV 19 の非互換回避）。"""
    import wave

    import numpy as np

    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--language", default="ja")
    p.add_argument("--device", choices=["cuda", "cpu"], required=True)
    p.add_argument("--ffmpeg", default="ffmpeg")
    a = p.parse_args(argv)
    # ライブラリ既定キャッシュ・取得用トークンを継承しない。モデルの自動取得は行わない（オフライン固定）。
    os.environ["HF_HOME"] = str(Path(a.model_dir) / ".hf-home")
    os.environ["HF_HUB_OFFLINE"] = "1"
    apply_hf_token(None)
    try:
        if a.device == "cuda":
            preload_cuda_libs()
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError:
        emit(status="blocked", reason="speech_extra_missing")
        return 2
    if a.device == "cuda" and ctranslate2.get_cuda_device_count() < 1:
        emit(status="blocked", reason="cuda_unavailable")
        return 2
    try:
        from faster_whisper.utils import download_model

        try:
            model_path = download_model(a.model, cache_dir=a.model_dir, local_files_only=True)
        except Exception:
            emit(status="blocked", reason="model_missing")
            return 2
        compute = "float16" if a.device == "cuda" else "int8"
        model = WhisperModel(model_path, device=a.device, compute_type=compute)
    except Exception as exc:  # モデル読込失敗（VRAM不足等）
        emit(status="blocked", reason="model_load_failed", detail=type(exc).__name__ + ": " + str(exc)[:200])
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="lrl-speech-") as td:
            wav = str(Path(td) / "audio.wav")
            extract_audio(a.video, wav, a.ffmpeg)
            if Path(wav).stat().st_size < 1000:
                emit(status="no_audio")
                Path(a.out).write_text(json.dumps({"segments": [], "no_audio": True}), "utf-8")
                return 0
            audio = load_wav(wav)
            segments, info = model.transcribe(audio, language=a.language, temperature=0.0, vad_filter=True,
                                              condition_on_previous_text=False, beam_size=5)
            out = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip(),
                    "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob} for s in segments]
        Path(a.out).write_text(json.dumps({"segments": out, "duration": float(info.duration), "language": info.language},
                                          ensure_ascii=False), "utf-8")
        emit(status="ok", segments=len(out), duration=float(info.duration))
        return 0
    except Exception as exc:
        emit(status="failed", reason=type(exc).__name__, detail=str(exc)[:200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
