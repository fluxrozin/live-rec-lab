"""通常設定の定義と検証器（要求§11.8.1）。UI/API/CLIで共有する。"""

from __future__ import annotations

import math
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_INT64 = 2**63 - 1


class SettingError(ValueError):
    pass


def _finite_number(v: Any, lo: float, hi: float, *, lo_open: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SettingError("数値で指定してください")
    if not math.isfinite(v):
        raise SettingError("有限の数値で指定してください")
    if (v <= lo) if lo_open else (v < lo):
        raise SettingError(f"{lo}{'より大きい' if lo_open else '以上'}の値を指定してください")
    if v > hi:
        raise SettingError(f"{hi}以下の値を指定してください")
    return v


def _integer(v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise SettingError("整数で指定してください")
    if v < lo or v > hi:
        raise SettingError(f"{lo}〜{hi}の整数で指定してください")
    return v


def _boolean(v: Any) -> bool:
    if not isinstance(v, bool):
        raise SettingError("true/falseで指定してください")
    return v


def _enum(*choices: str) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if not isinstance(v, str) or v not in choices:
            raise SettingError("次のいずれかを指定してください: " + ", ".join(choices))
        return v

    return check


def _engine_order(v: Any) -> list[str]:
    allowed = ("streamlink", "yt-dlp", "tiktoklive")
    if not isinstance(v, list) or not v or any(not isinstance(x, str) or x not in allowed for x in v):
        raise SettingError("streamlink / yt-dlp / tiktoklive の非空の部分列で指定してください")
    if len(set(v)) != len(v):
        raise SettingError("重複なく指定してください")
    return list(v)


def _timezone(v: Any) -> str:
    if not isinstance(v, str) or not v:
        raise SettingError("IANAタイムゾーン名で指定してください")
    try:
        zoneinfo.ZoneInfo(v)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise SettingError("解決できないタイムゾーンです") from exc
    return v


def _language(v: Any) -> str:
    if not isinstance(v, str) or not (2 <= len(v) <= 8) or not v.replace("-", "").isalpha():
        raise SettingError("言語コードで指定してください")
    return v.lower()


def _recording_root(v: Any) -> str:
    if not isinstance(v, str) or not v.startswith("/") or "\x00" in v or len(v) > 1024:
        raise SettingError("絶対パスで指定してください")
    return v


@dataclass(frozen=True)
class Definition:
    key: str
    category: str
    default: Any
    validate: Callable[[Any], Any]
    restart: bool = False  # 次回起動で反映


DEFINITIONS: dict[str, Definition] = {
    d.key: d
    for d in [
        Definition("tiktok_monitor_rate", "monitor", 10, lambda v: _finite_number(v, 0, 10, lo_open=True)),
        Definition("twitcasting_monitor_rate", "monitor", 10, lambda v: _finite_number(v, 0, 60, lo_open=True)),
        Definition("max_concurrent_recordings", "recording", 3, lambda v: _integer(v, 1, 20)),
        Definition("recording_quality", "recording", "best", _enum("best", "high", "medium")),
        Definition("recording_root", "recording", None, _recording_root),
        Definition("min_free_gib", "recording", 5, lambda v: _finite_number(v, 0, 1_000_000)),
        Definition("max_recording_gib", "recording", 0, lambda v: _finite_number(v, 0, 1_000_000)),
        Definition("archive_retention_days", "recording", 0, lambda v: _integer(v, 0, 36_500)),
        Definition("auto_delete_enabled", "recording", False, _boolean),
        Definition("tiktok_engine_order", "recording", ["streamlink"], _engine_order),  # yt-dlpは現行TikTokで利用不可（実測）のため既定から除外
        Definition("reconnect_max_attempts", "recording", 5, lambda v: _integer(v, 0, 30)),
        Definition("comments_globally_enabled", "comments", True, _boolean),
        Definition("twitcasting_comment_poll_seconds", "comments", 10, lambda v: _integer(v, 5, 120)),
        Definition("comment_srt_duration_seconds", "comments", 4, lambda v: _finite_number(v, 0.001, 3600, lo_open=False)),
        Definition("speech_subtitles_enabled", "speech", True, _boolean),
        Definition("speech_model", "speech", "large-v3", _enum("large-v3", "large-v3-turbo")),
        Definition("speech_language", "speech", "ja", _language),
        Definition("speech_device", "speech", "cuda", _enum("cuda", "cpu")),
        Definition("display_timezone", "display", "Asia/Tokyo", _timezone),
        Definition("display_font", "display", "windows", _enum("windows", "noto-sans-jp")),
        Definition("web_refresh_interval_seconds", "display", 5, lambda v: _integer(v, 1, 86_400)),
        Definition("log_level", "advanced", "INFO", _enum("DEBUG", "INFO", "WARNING", "ERROR")),
        Definition("web_port", "advanced", 8082, lambda v: _integer(v, 1024, 65535), restart=True),
        Definition("streamer_order_mode", "internal", "default", _enum("default", "custom")),
        Definition("streamer_order_revision", "internal", 1, lambda v: _integer(v, 1, MAX_INT64)),
    ]
}

# 通常設定として公開しない内部キー
INTERNAL_KEYS = {"streamer_order_mode", "streamer_order_revision"}


def validate_value(key: str, value: Any) -> Any:
    d = DEFINITIONS.get(key)
    if d is None:
        raise SettingError("未知の設定キーです")
    return d.validate(value)
