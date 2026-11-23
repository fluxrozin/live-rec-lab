"""保存パスと配信者 ID の正規化。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

_TIKTOK_ID = re.compile(r"^[A-Za-z0-9._]{1,64}$")
_TWITCAST_ID = re.compile(r"^(?:c:)?[A-Za-z0-9_]{1,64}$")
_TIKTOK_PATH = re.compile(r"^/@([A-Za-z0-9._]{1,64})(?:/live)?/?$")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}
_RECORDING_ROOTS_ENV = "LIVE_REC_LAB_EXISTING_RECORDING_ROOTS"
_FOLDER_MARKER = ".live-rec-lab-owner"


@dataclass(frozen=True)
class AccountInput:
    platform_user_id: str
    profile_url: str | None


def normalize_account(platform: str, raw: str) -> AccountInput:
    text = raw.strip()
    if platform == "tiktok":
        is_url = "://" in text
        if is_url:
            url = urlsplit(text)
            match = _TIKTOK_PATH.fullmatch(url.path)
            if url.scheme != "https" or url.netloc.lower() != "www.tiktok.com" or match is None:
                raise ValueError("TikTok のプロフィールURLを確認してください")
            text = match.group(1)
        else:
            text = text.removeprefix("@")
        if not _TIKTOK_ID.fullmatch(text) or not text.strip(".") or ".." in text:
            raise ValueError("TikTok のユーザー ID を確認してください")
        return AccountInput(text.lower(), f"https://www.tiktok.com/@{text.lower()}" if is_url else None)
    if platform == "twitcasting":
        if not _TWITCAST_ID.fullmatch(text):
            raise ValueError("ツイキャスのユーザー ID を確認してください")
        return AccountInput(text.lower(), None)
    raise ValueError("サービスを確認してください")


def safe_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unicodedata.normalize("NFKC", value)).strip(" .")
    cleaned = cleaned[:64].rstrip(" .")
    if not cleaned or cleaned.split(".")[0].rstrip().upper() in _RESERVED:
        raise ValueError("ファイル名に使えない ID です")
    return cleaned


def windows_name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().rstrip(" .")


def matching_windows_name(path: Path) -> Path | None:
    key = windows_name_key(path.name)
    try:
        with os.scandir(path.parent) as entries:
            return next((path.parent / entry.name for entry in entries if windows_name_key(entry.name) == key), None)
    except FileNotFoundError:
        return None


def streamer_directory(db: Session, root: Path, normalized_id: str) -> Path:
    """同じIDはサービスに関係なく同じフォルダを使う。呼出側でDB書込を直列化する。"""
    from live_rec_lab.db.models import StreamerFolder

    try:
        basename = safe_component(normalized_id)
    except ValueError:
        basename = "_" + (re.sub(r"[^A-Za-z0-9_-]", "_", normalized_id) or "account")[:48]
    folder = db.get(StreamerFolder, normalized_id)
    if folder is not None:
        path = safe_managed_path(root, root / folder.directory_name)
        _verify_folder(path, folder.owner_token)
        return path

    reserved = basename.startswith("_") and basename != normalized_id
    token = hashlib.sha256(normalized_id.encode()).hexdigest()
    candidates = [f"{basename[:48]}_{token[:n]}" for n in range(8, 65, 4)]
    if not reserved:
        candidates.insert(0, basename)
    for name in candidates:
        key = windows_name_key(name)
        if db.scalar(select(StreamerFolder.normalized_id).where(StreamerFolder.windows_key == key)) is not None:
            continue
        path = safe_managed_path(root, root / name)
        if matching_windows_name(path) is not None:
            raise ValueError(f"管理外の配信者フォルダを確認してください: {path}")
        owner = uuid4().hex
        _verify_folder(path, owner)
        db.add(StreamerFolder(normalized_id=normalized_id, directory_name=name, windows_key=key, owner_token=owner))
        db.flush()
        return path
    raise ValueError("配信者フォルダ名を一意に予約できません")


def _verify_folder(path: Path, token: str) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        marker = safe_managed_path(path, path / _FOLDER_MARKER)
        if not marker.is_file() or marker.stat().st_size != len(token):
            raise ValueError(f"管理外の配信者フォルダを確認してください: {path}") from None
        if marker.read_text(encoding="ascii", errors="replace") != token:
            raise ValueError(f"配信者フォルダの所有情報が一致しません: {path}") from None
    else:
        with (path / _FOLDER_MARKER).open("x", encoding="ascii") as marker:
            marker.write(token)
            marker.flush()
            os.fsync(marker.fileno())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def broadcast_filename_time(platform_started_at: datetime | None, detected_at: datetime) -> datetime:
    """ファイル名の時刻は配信開始。未取得のときは、その配信の検知時刻。"""
    return _as_utc(platform_started_at or detected_at)


def recording_paths(
    root: Path,
    platform: str,
    platform_user_id: str,
    started_at: datetime,
    timezone_name: str,
    segment_number: int,
) -> tuple[Path, Path, Path]:
    """日付フォルダとファイル名の時刻は、渡した配信の開始時刻。セグメントは番号だけが変わる。"""
    local = _as_utc(started_at).astimezone(ZoneInfo(timezone_name))
    folder = root / safe_component(platform_user_id) / local.strftime("%Y-%m-%d")
    stamp = local.strftime("%Y%m%d_%H%M%S")
    base = f"{platform}_{safe_component(platform_user_id)}_{stamp}_s{segment_number:03d}"
    candidate = folder / f"{base}.mp4"
    temp = candidate.with_name(candidate.stem + ".part.ts")
    return folder, temp, candidate


def _mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _windows_path_key(path: Path, *, require_windows: bool) -> tuple[str, ...]:
    if not require_windows:
        return tuple(windows_name_key(part) for part in path.parts)
    mounts: list[tuple[Path, str, str, str, str]] = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields, filesystem = before.split(), after.split()
        mount = Path(_mount_field(fields[4]))
        if path.is_relative_to(mount):
            mounts.append((mount, _mount_field(fields[3]), *[_mount_field(value) for value in filesystem[:3]]))
    if mounts:
        mount, inside, kind, source, options = max(mounts, key=lambda item: len(item[0].parts))
        if kind == "drvfs" or kind == "9p" and "drvfs" in options.casefold():
            windows = PureWindowsPath(source)
            if re.fullmatch(r"[a-zA-Z]:", windows.drive) and windows.is_absolute():
                parts = (*windows.parts[1:], *Path(inside).parts[1:], *path.relative_to(mount).parts)
                return (windows.drive.lower(), *(windows_name_key(part) for part in parts))
    raise ValueError(f"Windowsローカルドライブのマウントを確認してください: {path}")


def recording_root_status(root: Path, *, require_windows: bool = True) -> Path:
    root = root.expanduser()
    if not root.is_absolute():
        raise ValueError("保存先は絶対パスで指定してください")
    known = os.environ.get(_RECORDING_ROOTS_ENV, "").strip()
    if not known:
        raise ValueError(
            f"既存LiveRecorder・別インスタンスの実効録画先を確認し {_RECORDING_ROOTS_ENV} に設定してください"
        )
    root = root.resolve()
    key = _windows_path_key(root, require_windows=require_windows)
    if known != "none":
        for value in known.split(";"):
            other = Path(value.strip()).expanduser()
            if not other.is_absolute():
                raise ValueError("比較対象の実効録画先を絶対パスで設定してください")
            other_key = _windows_path_key(other.resolve(), require_windows=require_windows)
            size = min(len(key), len(other_key))
            if key[:size] == other_key[:size]:
                raise ValueError(f"録画先が重複しています: {root} / {other}。試作専用の別パスを設定してください")
    return root


def ensure_recording_root(path: Path, *, require_windows: bool = True) -> Path:
    root = recording_root_status(path, require_windows=require_windows)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=root, prefix=".live-rec-lab-", suffix=".probe"):
        pass
    return root


def safe_managed_path(root: Path, candidate: Path) -> Path:
    """既知ルートからの各構成要素を確認し、シンボリックリンクを拒否する。"""
    if not root.is_absolute() or not candidate.is_absolute():
        raise ValueError("管理パスは絶対パスで指定してください")
    if root.resolve() != root:
        raise ValueError("管理ルートにシンボリックリンクがあります")
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError("保存ルート外のパスです") from None
    if not relative.parts or ".." in relative.parts:
        raise ValueError("保存ルート外のパスです")
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"管理パスにシンボリックリンクがあります: {current}")
        if index + 1 < len(relative.parts) and not stat.S_ISDIR(mode):
            raise ValueError(f"管理パスの途中がディレクトリではありません: {current}")
    return candidate


def publish_exclusive(source: Path, target: Path) -> None:
    """同一ボリュームへ排他的に公開し、DB確定まで元ファイルを残す。"""
    identity = source.lstat()
    if (source.parent != target.parent or source.parent.resolve() != source.parent
            or not stat.S_ISREG(identity.st_mode)):
        raise ValueError("同一フォルダ内の管理ファイルだけ公開できます")
    if matching_windows_name(target) is not None:
        raise FileExistsError(target)
    os.link(source, target, follow_symlinks=False)
    # 大小文字を区別するディレクトリでも、同名の競合物を公開結果として扱わない。
    with os.scandir(target.parent) as entries:
        key = windows_name_key(target.name)
        duplicate = any(entry.name != target.name and windows_name_key(entry.name) == key for entry in entries)
    if duplicate:
        linked = target.lstat()
        if (linked.st_dev, linked.st_ino) == (identity.st_dev, identity.st_ino):
            target.unlink()
        raise FileExistsError(target)


def is_under(root: Path, candidate: Path) -> bool:
    try:
        safe_managed_path(root, candidate)
    except (ValueError, OSError):
        return False
    return True


def unlink_under(root: Path, candidate: str | None) -> None:
    if not candidate:
        return
    safe_managed_path(root, Path(candidate)).unlink(missing_ok=True)
