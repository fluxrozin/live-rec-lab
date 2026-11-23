"""現在の保存先と管理ファイルの同一性の照合。"""

from __future__ import annotations

import stat
from pathlib import Path

from sqlalchemy.orm import Session

from live_rec_lab.db.queries import load_settings


def configured_root(db: Session) -> Path:
    from live_rec_lab.storage import recording_root_status

    root = str(load_settings(db)["recording_root"])
    if not root:
        raise ValueError("録画保存先が未設定です")
    return recording_root_status(Path(root))


def managed_path(root: Path, name: str | Path) -> Path:
    from live_rec_lab.storage import safe_managed_path

    return safe_managed_path(root, Path(name))


def file_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise ValueError("通常の管理ファイルを照合できません")
    return info.st_dev, info.st_ino


def check_identity(path: Path, device: int | None, inode: int | None) -> None:
    if device is None or not inode or file_identity(path) != (device, inode):
        raise ValueError("管理ファイルの実体が記録と一致しません")
