"""Persist the fixed port for the next isolated server start."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import connect
from .isolation import FixtureWorkspace

FIXTURE_PORT = 8083


class PortRevisionConflict(ValueError):
    """A different request updated the port settings."""


@dataclass(frozen=True)
class WebPort:
    web_port: int = FIXTURE_PORT
    revision: int = 0


def validate_web_port(value: int) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("web_port must be an integer from 1024 to 65535")
    return value


def read_web_port(workspace: FixtureWorkspace) -> WebPort:
    workspace = FixtureWorkspace.open(workspace.root)
    workspace.require_ready()
    with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
        return _from_database(database)


def _from_database(database: sqlite3.Connection) -> WebPort:
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'system_settings'").fetchone():
        return WebPort()
    row = database.execute("SELECT value, revision FROM system_settings WHERE key = 'web_port'").fetchone()
    if row is None:
        return WebPort()
    port, revision = row
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid web_port settings revision")
    return WebPort(validate_web_port(json.loads(port)), revision)


def save_web_port(workspace: FixtureWorkspace, port: int, *, revision: int | None = None) -> WebPort:
    port = validate_web_port(port)
    with workspace.exclusive(), closing(connect(workspace.database)) as database, database:
        database.execute("BEGIN IMMEDIATE")
        current = _from_database(database)
        if revision is not None and current.revision != revision:
            raise PortRevisionConflict("web_port settings have changed")
        database.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        saved = WebPort(port, current.revision + 1)
        database.execute(
            """
            INSERT INTO system_settings (key, value, revision, updated_at) VALUES ('web_port', ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                revision=excluded.revision, updated_at=excluded.updated_at
        """,
            (json.dumps(port), saved.revision, datetime.now(UTC).isoformat()),
        )
        return saved
