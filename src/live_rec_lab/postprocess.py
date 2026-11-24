"""Exercise speech and ASS publication with an explicitly synthetic transcript."""

import sqlite3
from contextlib import closing

from .collection import FIXTURE_STREAMS, comment_cues, find_fixture_session
from .db import connect
from .isolation import FixtureWorkspace
from .media import fixture_dimensions
from .publication import matches_media, publish_initial_subtitle
from .speech import SpeechCue, render_combined_ass, render_speech_srt


def postprocess_fixture(workspace: FixtureWorkspace, platform: str) -> dict[str, object]:
    stream = FIXTURE_STREAMS[platform]
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        session = find_fixture_session(database, stream)
        if not session or session["stream_state"] != "ended" or session["recording_state"] != "completed":
            raise ValueError("subtitles require a completed fixture recording")
        if database.execute(
            "SELECT 1 FROM comment_expirations WHERE session_id = ?",
            (session["id"],),
        ).fetchone():
            raise ValueError("combined subtitles are blocked for expired or expiring comments")
        segment = database.execute(
            "SELECT * FROM recording_segments WHERE session_id = ? AND number = 1",
            (session["id"],),
        ).fetchone()
        if not segment or not matches_media(workspace, segment["video_path"], segment["video_generation"]):
            raise ValueError("video generation could not be verified")

        duration = segment["duration_ms"]
        width, height = fixture_dimensions(workspace.root / segment["video_path"])
        speech = [SpeechCue(300, 1100, "架空の発話です。")]
        comments = comment_cues(database, session["id"])
        combined = render_combined_ass(speech, comments, duration, width, height)
        speech_path = _publish(database, workspace, segment, "speech", render_speech_srt(speech, duration), 1)
        ass_path = _publish(database, workspace, segment, "ass", combined.text, 1 + len(comments))
        workspace.event("postprocess", "synthetic_results_available", stream.source_id)
        return {
            "mode": "fixture",
            "inference": "synthetic",
            "speech_srt": speech_path,
            "ass": ass_path,
            "ass_state": (
                "completed" if session["comment_state"] == "completed" and not combined.omitted_comments else "partial"
            ),
            "omitted_comments": combined.omitted_comments,
        }


def _publish(
    database: sqlite3.Connection, workspace: FixtureWorkspace, segment: sqlite3.Row, kind: str, body: str, version: int
) -> str:
    existing = database.execute(
        "SELECT * FROM subtitle_publications WHERE segment_id = ? AND kind = ?",
        (segment["id"], kind),
    ).fetchone()
    if existing is None:
        return str(publish_initial_subtitle(database, workspace, segment, kind, body, version))
    if existing["input_version"] != version or not matches_media(
        workspace,
        existing["relative_path"],
        existing["file_generation"],
    ):
        raise ValueError(f"existing {kind} subtitle generation requires review")
    return existing["relative_path"]
