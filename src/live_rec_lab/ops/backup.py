"""SQLite の一貫した手動バックアップ。"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

from live_rec_lab.config import load_paths


def backup_database(destination: Path) -> Path:
    paths = load_paths()
    source = paths.db_path
    destination = destination.expanduser()
    destination = destination.parent.resolve() / destination.name
    protected = {
        source, *(source.with_name(f"{source.name}-{suffix}") for suffix in ("wal", "shm", "journal")),
        paths.endpoint_path, paths.jobs_path, paths.lock_path, paths.data_dir / "instance.lock",
    }
    if destination in protected:
        raise ValueError("SQLite とアプリの実行時ファイルはバックアップ先に指定できません")
    if os.path.lexists(destination):
        raise FileExistsError(f"バックアップ先が既に存在します: {destination}")
    if not stat.S_ISREG(source.lstat().st_mode):
        raise ValueError("稼働用 SQLite が通常のファイルではありません")
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    deadline = time.monotonic() + 30

    def progress(_status: int, remaining: int, _pages: int) -> None:
        if remaining and time.monotonic() >= deadline:
            raise TimeoutError("SQLite のバックアップが制限時間内に完了しませんでした")

    try:
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=2)) as live,
            closing(sqlite3.connect(temporary)) as copy,
        ):
            live.backup(copy, pages=128, progress=progress, sleep=0.05)
        with temporary.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        os.link(temporary, destination, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
