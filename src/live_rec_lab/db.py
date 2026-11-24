"""SQLite schema for the first synthetic collection path."""

import sqlite3
from contextlib import closing
from pathlib import Path

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA user_version = 1;

CREATE TABLE streamers (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    normalized_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    display_name TEXT,
    recording_enabled INTEGER NOT NULL DEFAULT 1 CHECK (recording_enabled IN (0, 1)),
    comments_enabled INTEGER NOT NULL DEFAULT 1 CHECK (comments_enabled IN (0, 1)),
    archived_at TEXT,
    UNIQUE (platform, normalized_id)
);

CREATE TABLE stream_sessions (
    id INTEGER PRIMARY KEY,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id),
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    started_at TEXT NOT NULL,
    stream_state TEXT NOT NULL,
    recording_state TEXT NOT NULL DEFAULT 'pending',
    comment_state TEXT NOT NULL DEFAULT 'pending',
    subtitle_state TEXT NOT NULL DEFAULT 'pending',
    UNIQUE (streamer_id, source_id)
);

CREATE TABLE media_directories (
    relative_path TEXT PRIMARY KEY,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL
);

CREATE TABLE recording_segments (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES stream_sessions(id),
    number INTEGER NOT NULL CHECK (number >= 1),
    video_path TEXT NOT NULL UNIQUE,
    video_generation TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    UNIQUE (session_id, number)
);

CREATE TABLE comments (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES stream_sessions(id),
    source_id TEXT NOT NULL,
    posted_at TEXT,
    received_at TEXT NOT NULL,
    recording_offset_ms INTEGER,
    offset_source TEXT NOT NULL,
    author_id TEXT,
    author_name TEXT,
    body TEXT NOT NULL,
    UNIQUE (session_id, source_id)
);

CREATE TABLE comment_expirations (
    session_id INTEGER PRIMARY KEY REFERENCES stream_sessions(id),
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    removed_comments INTEGER
);

CREATE TABLE subtitle_publications (
    segment_id INTEGER NOT NULL REFERENCES recording_segments(id),
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    file_generation TEXT NOT NULL,
    input_version INTEGER NOT NULL,
    PRIMARY KEY (segment_id, kind)
);
"""


def initialize_database(path: Path) -> None:
    with path.open("xb"):
        pass
    path.chmod(0o600)
    with closing(connect(path)) as database:
        database.executescript(_SCHEMA)


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
