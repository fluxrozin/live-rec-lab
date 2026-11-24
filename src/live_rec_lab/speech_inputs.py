"""Recording eligibility shared by speech and ASS jobs."""

import sqlite3

from .isolation import Workspace
from .publication import matches_media
from .retention import media_deletion_status

SEGMENT = """
SELECT recording_segments.*, stream_sessions.stream_state, stream_sessions.recording_state,
       stream_sessions.comment_state, stream_sessions.subtitle_state, stream_sessions.source_id
FROM recording_segments JOIN stream_sessions ON stream_sessions.id = recording_segments.session_id
"""


def verified_segment(
    database: sqlite3.Connection,
    workspace: Workspace,
    segment_id: int,
    input_generation: str | None = None,
) -> sqlite3.Row:
    workspace.require_media()
    segment = database.execute(SEGMENT + " WHERE recording_segments.id = ?", (segment_id,)).fetchone()
    if segment is None:
        raise ValueError("recorded segment not found")
    if segment["stream_state"] != "ended" or segment["recording_state"] not in {"completed", "partial", "cancelled"}:
        raise ValueError("subtitles require a settled recording")
    if media_deletion_status(database, segment["session_id"]):
        raise ValueError("subtitles unavailable for deleted or deleting media")
    if (input_generation is not None and segment["video_generation"] != input_generation) or not matches_media(
        workspace, segment["video_path"], segment["video_generation"]
    ):
        raise ValueError("video generation changed")
    return segment
