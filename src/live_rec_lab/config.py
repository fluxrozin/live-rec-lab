"""実行時パスと設定値の規則。"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


def utcnow() -> datetime:
    return datetime.now(UTC)


def load_environment() -> None:
    env_file = Path.cwd() / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    twitcasting_client_id: str = ""
    twitcasting_client_secret: str = ""
    sign_api_key: str = ""


def load_secrets() -> Secrets:
    load_environment()
    return Secrets()


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    state_dir: Path
    db_path: Path
    log_dir: Path
    runtime_dir: Path
    endpoint_path: Path
    lock_path: Path
    jobs_path: Path

    def ensure(self) -> None:
        for directory in (self.data_dir, self.state_dir, self.log_dir, self.runtime_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(f"実行時ディレクトリの所有者と権限（0700）を確認してください: {directory}")


def load_paths() -> Paths:
    load_environment()
    home = Path.home()
    data = Path(
        os.environ.get(
            "LIVE_REC_LAB_DATA_DIR",
            home / ".local/share/live-rec-lab/data",
        )
    ).expanduser()
    state = Path(
        os.environ.get(
            "LIVE_REC_LAB_STATE_DIR",
            home / ".local/state/live-rec-lab",
        )
    ).expanduser()
    data = data.resolve()
    state = state.resolve()
    for label, path in (("データベース", data), ("状態ファイル", state)):
        if _on_windows_mount(path) or _on_shared_mount(path):
            raise ValueError(f"{label}は WSL の Linux 側に置いてください")
    runtime = state / "runtime"
    return Paths(
        data_dir=data,
        state_dir=state,
        db_path=data / "app.sqlite3",
        log_dir=state / "logs",
        runtime_dir=runtime,
        endpoint_path=runtime / "endpoint.json",
        lock_path=runtime / "instance.lock",
        jobs_path=runtime / "jobs.json",
    )


def _on_windows_mount(path: Path) -> bool:
    parts = path.parts
    return len(parts) >= 2 and parts[1] == "mnt"


def _on_shared_mount(path: Path) -> bool:
    filesystem = ""
    longest = 0
    mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="surrogateescape")
    for line in mounts.splitlines():
        location, _, description = line.partition(" - ")
        fields = location.split()
        if len(fields) < 5 or not description:
            continue
        mountpoint = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        depth = len(mountpoint.parts)
        if depth >= longest and path.is_relative_to(mountpoint):
            filesystem = description.split()[0]
            longest = depth
    return filesystem in {"9p", "drvfs", "cifs", "smb3", "nfs", "nfs4"} or filesystem.startswith("fuse.")


DEFAULTS: dict[str, object] = {
    "monitor_per_minute_tiktok": 10.0,
    "monitor_per_minute_twitcasting": 10.0,
    "max_parallel_recordings": 3,
    "recording_root": "",
    "min_free_bytes": 1073741824,
    "max_recording_bytes": 0,
    "retention_days": 0,
    "auto_delete_recordings": False,
    "comment_retention_days": 0,
    "end_grace_seconds": 15,
    "orphan_recheck_seconds": 120,
    "timezone": "Asia/Tokyo",
    "font": "windows",
    "srt_display_seconds": 4.0,
    "web_port": 0,
    "recording_quality": "best",
    "comments_enabled": True,
    "tiktok_engine_first": "streamlink",
    "log_level": "INFO",
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def validate_setting(key: str, value: object) -> object:
    if key not in DEFAULTS:
        raise ValueError("未知の設定です")
    if key == "monitor_per_minute_tiktok":
        number = float(value)
        if not 0 < number <= 10:
            raise ValueError("TikTok の監視回数は 0 より大きく 10 以下です")
        return number
    if key == "monitor_per_minute_twitcasting":
        number = float(value)
        if not 0 < number <= 60:
            raise ValueError("ツイキャスの監視回数は 0 より大きく 60 以下です")
        return number
    if key == "max_parallel_recordings":
        number = int(value)
        if not 1 <= number <= 8:
            raise ValueError("同時録画数は 1 から 8 です")
        return number
    if key == "recording_root":
        text = str(value).strip()
        if not text:
            return ""
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ValueError("保存先は絶対パスで指定してください")
        return str(path)
    if key == "min_free_bytes":
        number = int(value)
        if number < 0:
            raise ValueError("空き容量の下限は 0 以上です")
        return number
    if key == "max_recording_bytes":
        number = int(value)
        if not 0 <= number <= 2**63 - 1:
            raise ValueError("保存容量上限は 0 以上で指定してください")
        return number
    if key == "retention_days":
        number = int(value)
        if not 0 <= number <= 3650:
            raise ValueError("保存期限は 0 から 3650 日です")
        return number
    if key in {"comment_retention_days", "end_grace_seconds", "orphan_recheck_seconds"}:
        text = str(value)
        if not re.fullmatch(r"[0-9]+", text):
            raise ValueError("0 以上の整数を指定してください")
        number = int(text)
        if key != "comment_retention_days" and not 1 <= number <= 86400:
            raise ValueError("確認期限には 1〜86400 秒を指定してください")
        return number
    if key == "timezone":
        text = str(value).strip()
        try:
            ZoneInfo(text)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("タイムゾーンを確認してください") from exc
        return text
    if key == "font":
        text = str(value)
        if text not in {"windows", "noto"}:
            raise ValueError("フォントは Windows 標準または Noto Sans JP です")
        return text
    if key == "srt_display_seconds":
        number = float(value)
        if not 1 <= number <= 30:
            raise ValueError("字幕の表示秒数は 1 から 30 です")
        return number
    if key == "web_port":
        number = int(value)
        if not 0 <= number <= 65535:
            raise ValueError("ポートは 0 から 65535 です")
        return number
    if key == "recording_quality":
        text = str(value)
        if text not in {"best", "worst"}:
            raise ValueError("録画品質は best または worst です。再エンコードはしません")
        return text
    if key in {"comments_enabled", "auto_delete_recordings"}:
        text = str(value).lower()
        if text not in {"true", "false"}:
            raise ValueError("有効または無効を選択してください")
        return text == "true"
    if key == "tiktok_engine_first":
        text = str(value)
        if text not in {"streamlink", "yt-dlp"}:
            raise ValueError("最初に使用する録画エンジンを選択してください")
        return text
    if key == "log_level":
        text = str(value).upper()
        if text not in LOG_LEVELS:
            raise ValueError("ログレベルを選択してください")
        return text
    raise ValueError("未知の設定です")


def encode_setting(value: object) -> str:
    return str(value)


def decode_setting(key: str, raw: str) -> object:
    kind = DEFAULTS[key]
    if isinstance(kind, bool):
        return raw == "True"
    if isinstance(kind, int) and not isinstance(kind, bool):
        return int(raw)
    if isinstance(kind, float):
        return float(raw)
    return raw
