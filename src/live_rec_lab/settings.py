"""Versioned monitoring, media and retention preferences."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path

from .application import ApplicationWorkspace
from .db import connect
from .isolation import Workspace


@dataclass(frozen=True)
class OperationsSettings:
    tiktok_per_minute: float = 10
    twitcasting_per_minute: float = 10
    recording_root: str | None = None
    comment_retention_days: int = 0
    video_retention_days: int = 0
    minimum_free_gib: float = 5
    maximum_recording_gib: float = 0
    auto_delete_enabled: bool = False
    comments_globally_enabled: bool = True
    revision: int = 0

    def __post_init__(self) -> None:
        limits = ((self.tiktok_per_minute, 10), (self.twitcasting_per_minute, 60))
        retention = (self.comment_retention_days, self.video_retention_days)
        capacity = (self.minimum_free_gib, self.maximum_recording_gib)
        if any(
            type(value) not in (int, float) or not isfinite(value) or not 0 < value <= high for value, high in limits
        ):
            raise ValueError("monitoring interval is outside its allowed range")
        if any(type(value) is not int or not 0 <= value <= 36500 for value in retention):
            raise ValueError("retention setting is outside its allowed range")
        if any(
            type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1_000_000 for value in capacity
        ):
            raise ValueError("capacity is outside its allowed range")
        if 0 < self.maximum_recording_gib < 1 / 2**30:
            raise ValueError("maximum recording size is less than one byte")
        if type(self.auto_delete_enabled) is not bool or type(self.comments_globally_enabled) is not bool:
            raise ValueError("invalid operations flags")
        if type(self.revision) is not int or not 0 <= self.revision < 2**53 - 1:
            raise ValueError("invalid settings revision")
        if self.recording_root is not None:
            path = Path(self.recording_root)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("invalid recording root")


class OperationsConflict(ValueError):
    """Saving these settings would conflict with current state."""


def _read(database: sqlite3.Connection) -> OperationsSettings:
    if not database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations_preferences'"
    ).fetchone():
        return OperationsSettings()
    row = database.execute("SELECT value FROM operations_preferences WHERE id=1").fetchone()
    return OperationsSettings(**json.loads(row[0])) if row else OperationsSettings()


def read_operations_settings(workspace: Workspace) -> OperationsSettings:
    workspace.recheck()
    workspace.require_ready()
    with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
        return _read(database)


def save_operations_settings(
    workspace: Workspace, settings: OperationsSettings, *, confirmed: bool = False
) -> OperationsSettings:
    with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        old = _read(database)
        if old.revision != settings.revision:
            raise OperationsConflict("settings revision changed")
        if not confirmed and (
            (
                settings.auto_delete_enabled
                and (
                    not old.auto_delete_enabled
                    or _shortened(settings.video_retention_days, old.video_retention_days)
                    or _shortened(settings.maximum_recording_gib, old.maximum_recording_gib)
                    or settings.minimum_free_gib > old.minimum_free_gib
                )
            )
            or _shortened(settings.comment_retention_days, old.comment_retention_days)
        ):
            raise OperationsConflict("retention expansion requires confirmation")
        if settings.recording_root != old.recording_root:
            if not isinstance(workspace, ApplicationWorkspace) or workspace.mode != "normal":
                raise OperationsConflict("the isolated root is configured before startup")
            workspace.check_windows_recording_root(
                Path(settings.recording_root) if settings.recording_root else workspace.configured_recordings
            )
            if database.execute("SELECT 1 FROM recording_segments LIMIT 1").fetchone():
                raise OperationsConflict("recording root change needs a storage migration")
        saved = OperationsSettings(**{**asdict(settings), "revision": old.revision + 1})
        database.execute("""
            CREATE TABLE IF NOT EXISTS operations_preferences (
                id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL
            )
        """)
        database.execute(
            "INSERT INTO operations_preferences VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
            (json.dumps(asdict(saved), ensure_ascii=False),),
        )
        return saved


def _shortened(new: float, old: float) -> bool:
    return new > 0 and (old == 0 or new < old)
