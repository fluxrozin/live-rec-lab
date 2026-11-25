"""Windows保存パス・名前生成・非上書き確定（要求§10.2〜10.3、システム設計§13.2）。

実測（2026-11-25, WSL2 /mnt/d = 9p drvfs）:
* renameat2(RENAME_NOREPLACE) は EINVAL で使えない。
* os.link(tmp, final) は原子的に非上書きで、大小文字違いの既存名にも EEXIST になる。
* os.rename は既存を置換するため新規確定には使わない。
よって新規確定は「同一ボリュームの一時ファイル → link → 一時unlink」とする。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)}, *{f"lpt{i}" for i in range(1, 10)}}
MAX_PATH = 240  # Windowsのパス長（260）に余裕を持たせる
COMPONENT_BYTES = 230  # 1要素の最大バイト数（255 - 一時名の付加分）
TMP_SUFFIX = ".lrl-tmp"


class StorageError(RuntimeError):
    pass


class AlreadyExists(StorageError):
    pass


def safe_component(text: str, *, fallback: str = "untitled") -> str:
    """Windowsで使える1要素名へ安全化（NFC、禁止文字→_、末尾の空白/ピリオド除去、予約名回避）。"""
    s = unicodedata.normalize("NFC", text or "")
    s = _FORBIDDEN.sub("_", s).strip().rstrip(". ").strip()
    if s in ("", ".", ".."):
        return fallback
    if s.split(".")[0].lower() in _RESERVED:
        s = "_" + s
    return s


def shorten(text: str, max_chars: int) -> str:
    """Unicode文字単位で短縮（結合文字列を壊さないようNFC済みの文字数で切る）。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip(". ").strip() or "untitled"


def folder_token(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]


def session_token(session_id: int) -> str:
    return f"q{session_id:x}"


def filename_datetime(when_utc: datetime, tz_name: str) -> str:
    return when_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y%m%d_%H%M%S")


def date_folder(when_utc: datetime, tz_name: str) -> str:
    return when_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def build_stem(platform: str, streamer_dir: str, fname_dt: str, title: str, number: int, disambiguator: str | None,
               *, room: int) -> str:
    """動画stem。roomは「ディレクトリ+接尾辞込みで残せる文字数」。タイトルだけを短縮する。"""
    seg = f"s{number:03d}"
    tail = f"_{disambiguator}_{seg}" if disambiguator else f"_{seg}"
    head = f"{platform}_{streamer_dir}_{fname_dt}_"
    avail = room - len(head) - len(tail)
    if avail < 1:
        raise StorageError("パス長の制約で名前を確定できません（保留）")
    t = shorten(title, avail)
    # 1要素は255バイト以内（ext4隔離領域）。一時名の接頭・接尾(18B)と最長接尾辞を差し引く
    while len((head + t + tail + "_comment.srt").encode("utf-8")) > COMPONENT_BYTES and len(t) > 1:
        t = t[:-1].rstrip(". ").strip() or t[:-1]
    if len((head + t + tail + "_comment.srt").encode("utf-8")) > COMPONENT_BYTES:
        raise StorageError("名前が長すぎて確定できません（保留）")
    return head + t + tail


SUFFIXES = {"video": ".mp4", "comment_srt": "_comment.srt", "speech_srt": "_speech.srt", "ass": ".ass"}


def stem_room(root: Path, streamer_dir: str, date_dir: str) -> int:
    """stemに使える最大文字数（最長の接尾辞 `_comment.srt` と区切りを考慮）。"""
    base = len(str(root)) + 1 + len(streamer_dir) + 1 + len(date_dir) + 1
    return max(0, MAX_PATH - base - len("_comment.srt"))


def identity(path: Path) -> dict:
    st = os.stat(path)
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def same_identity(a: dict, b: dict, *, check_mtime: bool = True) -> bool:
    keys = ["dev", "ino", "size"] + (["mtime_ns"] if check_mtime else [])
    return all(a.get(k) == b.get(k) for k in keys)


def new_tmp_path(final: Path) -> Path:
    """最終ファイルと同一ディレクトリ（同一ボリューム）の一意な一時ファイル名。"""
    return final.with_name(f".{final.name}.{uuid.uuid4().hex[:8]}{TMP_SUFFIX}")


def commit_new(tmp: Path, final: Path) -> dict:
    """一時ファイルを非上書きで正式名へ確定する。既存があれば AlreadyExists（一時は保全）。"""
    if tmp.parent != final.parent:
        raise StorageError("一時ファイルは最終ファイルと同一ディレクトリに置く必要があります")
    before = identity(tmp)
    try:
        os.link(tmp, final)
    except FileExistsError as exc:
        raise AlreadyExists(str(final)) from exc
    except OSError as exc:
        raise StorageError(f"非上書き確定に失敗（一時ファイルを保全）: {exc}") from exc
    after = identity(final)
    if not (after["dev"] == before["dev"] and after["ino"] == before["ino"] and after["size"] == before["size"]):
        # 想定外: 別実体になっている。確定を取り消さず異常として扱う
        raise StorageError("確定後の実体が一時ファイルと一致しません")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    return identity(final)


def create_text_new(final: Path, data: str, encoding: str = "utf-8") -> dict:
    """字幕の初回作成: 一時ファイル→非上書き確定。ASSはBOM付き(utf-8-sig)で書く（MPC-HC等の描画フィルタの文字コード判定対策）。"""
    tmp = new_tmp_path(final)
    tmp.write_text(data, encoding=encoding, newline="")
    try:
        return commit_new(tmp, final)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def replace_managed(final: Path, data: str, expected: dict | None, encoding: str = "utf-8") -> dict:
    """管理済み字幕の再生成置換。記録済みの世代と一致する場合のみ置換する。"""
    if not final.exists():
        raise StorageError("置換対象が存在しません")
    if expected is None or not same_identity(identity(final), expected):
        raise StorageError("管理外の変更または別世代のため置換しません（保留）")
    tmp = new_tmp_path(final)
    tmp.write_text(data, encoding=encoding, newline="")
    try:
        os.replace(tmp, final)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return identity(final)


@dataclass
class RootStatus:
    ok: bool
    reason: str | None
    free_bytes: int | None = None


def check_root(root: Path, *, create: bool = False) -> RootStatus:
    """保存ルートの利用可否（マウント・書込・容量）。未接続を不在と誤認しない。"""
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            return RootStatus(False, "保存ルートが存在しないか未接続です")
        probe = root / f".lrl-probe-{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"x")
        probe.unlink()
        return RootStatus(True, None, shutil.disk_usage(root).free)
    except OSError as exc:
        return RootStatus(False, f"書込できません: {exc}")
