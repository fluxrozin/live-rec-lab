"""Validated account registry backed by the collection database."""

import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from .providers.target import normalize_target

ACCOUNT_COLUMNS = "id, platform, normalized_id, display_name, enabled, recording_enabled, comments_enabled"


class DuplicateAccount(ValueError):
    """The service account is already active."""


@dataclass(frozen=True)
class StreamerSettings:
    display_name: str | None
    monitoring_enabled: bool
    recording_enabled: bool
    comments_enabled: bool

    def values(self) -> tuple[str | None, bool, bool, bool]:
        name = self.display_name or ""
        if len(name) > 100 or any(unicodedata.category(character).startswith("C") for character in name):
            raise ValueError("invalid display name")
        return name.strip() or None, self.monitoring_enabled, self.recording_enabled, self.comments_enabled


def account_view(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data["display_name"] = data["display_name"] or data["normalized_id"]
    data["monitoring_enabled"] = bool(data.pop("enabled"))
    data["recording_enabled"] = bool(data["recording_enabled"])
    data["comments_enabled"] = bool(data["comments_enabled"])
    return data


def register(database: sqlite3.Connection, platform: str, target: str, settings: StreamerSettings) -> dict:
    normalized = normalize_target(platform, target)
    with database:
        found = database.execute(
            f"""
            INSERT INTO streamers (platform, normalized_id, display_name, enabled, recording_enabled, comments_enabled)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, normalized_id) DO UPDATE SET
              display_name = excluded.display_name, enabled = excluded.enabled,
              recording_enabled = excluded.recording_enabled, comments_enabled = excluded.comments_enabled,
              archived_at = NULL
            WHERE streamers.archived_at IS NOT NULL
            RETURNING {ACCOUNT_COLUMNS}
            """,
            (platform, normalized, *settings.values()),
        ).fetchone()
    if found is None:
        raise DuplicateAccount("account is already registered")
    return account_view(found)


def update(database: sqlite3.Connection, streamer_id: int, settings: StreamerSettings) -> dict | None:
    with database:
        found = database.execute(
            f"""
            UPDATE streamers SET display_name = ?, enabled = ?, recording_enabled = ?, comments_enabled = ?
            WHERE id = ? AND archived_at IS NULL
            RETURNING {ACCOUNT_COLUMNS}
            """,
            (*settings.values(), streamer_id),
        ).fetchone()
    return account_view(found) if found is not None else None


def archive(database: sqlite3.Connection, streamer_id: int) -> bool:
    with database:
        result = database.execute(
            "UPDATE streamers SET archived_at = ?, enabled = 0 WHERE id = ? AND archived_at IS NULL",
            (datetime.now(UTC).isoformat(), streamer_id),
        )
    return result.rowcount == 1
