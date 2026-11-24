"""Read models for the isolated browser and JSON views."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from ..isolation import FixtureWorkspace

session_select = """
SELECT stream_sessions.id, streamer_id, source_id, platform, normalized_id, streamers.display_name,
       title, started_at, stream_state, recording_state, comment_state, subtitle_state
FROM stream_sessions JOIN streamers ON streamers.id = streamer_id
"""

_comments_select = """
SELECT comments.id, session_id, author_id, author_name, body, posted_at, received_at, recording_offset_ms,
       stream_sessions.title AS session_title
FROM comments JOIN stream_sessions ON stream_sessions.id = comments.session_id
WHERE NOT EXISTS (SELECT 1 FROM comment_expirations WHERE comment_expirations.session_id = comments.session_id)
  AND (? IS NULL OR session_id = ?)
  AND (? = '' OR body LIKE ? ESCAPE '!' OR author_id LIKE ? ESCAPE '!' OR author_name LIKE ? ESCAPE '!')
"""


@contextmanager
def read_database(workspace: FixtureWorkspace) -> Iterator[sqlite3.Connection]:
    checked = FixtureWorkspace.open(workspace.root)
    with closing(sqlite3.connect(checked.database.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only = ON")
        database.execute("BEGIN")
        yield database


def page_rows(database: sqlite3.Connection, statement: str, args: tuple, order: str, page: int, page_size: int) -> dict:
    total = database.execute(f"SELECT count(*) FROM ({statement})", args).fetchone()[0]
    rows = database.execute(
        f"SELECT * FROM ({statement}) ORDER BY {order} LIMIT ? OFFSET ?",
        (*args, page_size, (page - 1) * page_size),
    )
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < total,
        "as_of": datetime.now(UTC).isoformat(),
    }


def like_pattern(term: str) -> str:
    return "%" + term.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def comment_page(database: sqlite3.Connection, session_id: int | None, term: str, order: str, page: int, size: int):
    search = like_pattern(term)
    direction = "ASC" if order == "asc" else "DESC"
    result = page_rows(
        database,
        _comments_select,
        (session_id, session_id, term, search, search, search),
        f"coalesce(posted_at, received_at) {direction}, id {direction}",
        page,
        size,
    )
    if (
        result["items"]
        and database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'comment_provider_attributes'"
        ).fetchone()
    ):
        ids = [item["id"] for item in result["items"]]
        placeholders = ",".join("?" for _ in ids)
        attributes = {
            row["comment_id"]: row
            for row in database.execute(
                f"SELECT * FROM comment_provider_attributes WHERE comment_id IN ({placeholders})",
                ids,
            )
        }
        for item in result["items"]:
            fields = attributes.get(item["id"])
            if fields:
                item.update(is_anonymous=bool(fields["is_anonymous"]), anonymous_number=fields["anonymous_number"])
    return result


def history_page(
    database: sqlite3.Connection,
    platform: str | None,
    streamer_id: int | None,
    term: str,
    page: int,
    size: int,
):
    search = like_pattern(term)
    statement = (
        session_select
        + """
    WHERE (? IS NULL OR platform = ?) AND (? IS NULL OR streamer_id = ?)
    AND (? = '' OR title LIKE ? ESCAPE '!' OR normalized_id LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!')
    """
    )
    return page_rows(
        database,
        statement,
        (platform, platform, streamer_id, streamer_id, term, search, search, search),
        "started_at DESC, id DESC",
        page,
        size,
    )
