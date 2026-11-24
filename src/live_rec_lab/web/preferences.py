"""Versioned display preferences. Old fixture databases use the defaults."""

import sqlite3
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DisplayPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font: Literal["windows", "noto"] = "windows"
    display_timezone: Annotated[str, Field(min_length=1, max_length=100)] = "Asia/Tokyo"
    refresh_seconds: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)] = 5
    revision: Annotated[int, Field(strict=True, ge=0, le=2**63 - 2)] = 0

    @field_validator("display_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("IANAタイムゾーンを確認してください") from error
        return value


class DisplayConflict(ValueError):
    """The settings were changed after the form was opened."""


def read_preferences(database: sqlite3.Connection) -> DisplayPreferences:
    if not database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fixture_display_preferences'"
    ).fetchone():
        return DisplayPreferences()
    row = database.execute(
        "SELECT font, display_timezone, refresh_seconds, revision FROM fixture_display_preferences WHERE id = 1"
    ).fetchone()
    return DisplayPreferences(**dict(row)) if row else DisplayPreferences()


def save_preferences(database: sqlite3.Connection, settings: DisplayPreferences) -> DisplayPreferences:
    with database:
        database.execute("BEGIN IMMEDIATE")
        database.execute("""
            CREATE TABLE IF NOT EXISTS fixture_display_preferences (
              id INTEGER PRIMARY KEY CHECK (id = 1), font TEXT NOT NULL, display_timezone TEXT NOT NULL,
              refresh_seconds INTEGER NOT NULL, revision INTEGER NOT NULL
            )
        """)
        current = read_preferences(database)
        if current.revision != settings.revision:
            raise DisplayConflict()
        revision = current.revision + 1
        database.execute(
            """
            INSERT INTO fixture_display_preferences VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET font = excluded.font, display_timezone = excluded.display_timezone,
            refresh_seconds = excluded.refresh_seconds, revision = excluded.revision
        """,
            (settings.font, settings.display_timezone, settings.refresh_seconds, revision),
        )
    return settings.model_copy(update={"revision": revision})
