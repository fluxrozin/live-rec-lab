import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from live_rec_lab import publication, retention
from live_rec_lab.collection import run_fixture
from live_rec_lab.db import connect
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.maintenance import expire_fixture_comments, inspect_fixture_restore
from live_rec_lab.postprocess import postprocess_fixture
from live_rec_lab.runtime import backup_fixture


def collected(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    recording = run_fixture(workspace)
    generated = postprocess_fixture(workspace, "tiktok")
    outputs = [
        workspace.root / output
        for output in (recording["video"], recording["comment_srt"], generated["speech_srt"], generated["ass"])
    ]
    assert all(path.is_file() for path in outputs)
    return workspace, outputs


def test_file_only_removes_all_media_and_preserves_history(tmp_path):
    workspace, outputs = collected(tmp_path)
    old_backup = backup_fixture(workspace)

    result = retention.delete_session_media(workspace, 1, "files")

    assert result["state"] == "completed"
    assert result["files"] == {"pending": 0, "removed": 4}
    assert all(not path.exists() for path in outputs)
    with closing(connect(workspace.database)) as database:
        assert database.execute("SELECT recording_state FROM stream_sessions").fetchone()[0] == "deleted"
        assert database.execute("SELECT count(*) FROM comments").fetchone()[0] == 2
        assert retention.deleted_source(database, 1, "synthetic-tiktok-001")
        assert retention.media_deletion_status(database, 1) == result
    assert run_fixture(workspace)["recording_state"] == "deleted"
    assert inspect_fixture_restore(workspace, old_backup.name)["state"] == "blocked"
    deleted_backup = backup_fixture(workspace)
    assert inspect_fixture_restore(workspace, deleted_backup.name)["state"] == "reviewable"


def test_history_removal_is_idempotent_and_original_source_cannot_return(tmp_path):
    workspace, outputs = collected(tmp_path)
    first = retention.delete_session_media(workspace, 1, "files")
    expire_fixture_comments(workspace, "tiktok")
    result = retention.delete_session_media(workspace, 1, "files_and_history")
    assert result["operation_id"] == first["operation_id"]
    assert result["state"] == "completed"
    assert retention.delete_session_media(workspace, 1, "files_and_history") == result
    assert all(not path.exists() for path in outputs)

    assert run_fixture(workspace)["recording_state"] == "deleted"
    with closing(connect(workspace.database)) as database:
        for table in ("stream_sessions", "recording_segments", "subtitle_publications", "comments"):
            assert database.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert retention.deleted_source(database, 1, "synthetic-tiktok-001")
        expiry = database.execute(
            "SELECT previous_expiration FROM media_deletion_operations WHERE mode = 'files_and_history'"
        ).fetchone()[0]
        assert json.loads(expiry)["removed_comments"] == 2


@pytest.mark.parametrize("column,state", [("stream_state", "interrupted"), ("recording_state", "needs_review")])
def test_deletion_rejects_unsettled_sources(tmp_path, column, state):
    workspace, outputs = collected(tmp_path)
    with closing(connect(workspace.database)) as database, database:
        database.execute(f"UPDATE stream_sessions SET {column} = ?", (state,))
    with pytest.raises(retention.MediaDeletionConflict):
        retention.delete_session_media(workspace, 1, "files_and_history")
    assert all(path.is_file() for path in outputs)


def test_unowned_subtitle_or_replaced_video_prevents_initial_deletion(tmp_path):
    workspace, outputs = collected(tmp_path)
    outputs[0].unlink()
    outputs[0].write_bytes(b"same name, different file")
    with pytest.raises(retention.MediaDeletionConflict):
        retention.delete_session_media(workspace, 1, "files")
    assert outputs[0].read_bytes() == b"same name, different file"
    assert all(path.is_file() for path in outputs[1:])


def test_unrecorded_casefold_subtitle_prevents_deletion(tmp_path):
    workspace, outputs = collected(tmp_path)
    alias = outputs[1].with_name(outputs[1].name.upper())
    alias.write_bytes(b"unowned alias")
    with pytest.raises(retention.MediaDeletionConflict):
        retention.delete_session_media(workspace, 1, "files")
    assert all(path.is_file() for path in [*outputs, alias])


def test_pending_delete_recovers_after_unlink_before_commit(tmp_path, monkeypatch):
    workspace, outputs = collected(tmp_path)
    real_unlink = retention._unlink_captured

    def interrupted(dir_fd):
        real_unlink(dir_fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(retention, "_unlink_captured", interrupted)
    with pytest.raises(KeyboardInterrupt):
        retention.delete_session_media(workspace, 1, "files")
    with closing(connect(workspace.database)) as database:
        operation = retention.media_deletion_status(database, 1)
        assert operation["state"] == "prepared" and operation["files"]["pending"] == 4
    monkeypatch.setattr(retention, "_unlink_captured", real_unlink)

    [result] = retention.resume_media_deletions(workspace)

    assert result["state"] == "completed"
    assert result["files"] == {"pending": 0, "removed": 4}
    assert all(not path.exists() for path in outputs)


def test_rename_race_preserves_the_replacement_and_requires_review(tmp_path, monkeypatch):
    workspace, outputs = collected(tmp_path)
    real_move = retention._move_no_replace
    moved = None
    moved_path = None

    def swapped(source_fd, source, target_fd, target):
        nonlocal moved, moved_path
        selected = Path(f"/proc/self/fd/{source_fd}").resolve() / source
        if moved is None:
            moved_path = next(path for path in outputs if path.name == source)
            moved = selected.with_name(source + ".manual-copy")
            selected.rename(moved)
            selected.write_bytes(b"external replacement")
        real_move(source_fd, source, target_fd, target)

    monkeypatch.setattr(retention, "_move_no_replace", swapped)
    report = retention.delete_session_media(workspace, 1, "files_and_history")

    assert report["state"] == "needs_review"
    assert moved is not None and moved.exists()
    assert moved_path is not None and moved_path.read_bytes() == b"external replacement"
    with closing(sqlite3.connect(workspace.database)) as database:
        assert database.execute("SELECT count(*) FROM stream_sessions").fetchone()[0] == 1


def test_capacity_retention_is_opt_in_and_selects_the_oldest_settled_recording(tmp_path):
    workspace, original = collected(tmp_path)
    other = run_fixture(workspace, "twitcasting")
    other_video = workspace.media_path(other["video"])
    now = datetime.now(UTC)
    with closing(connect(workspace.database)) as database, database:
        database.execute(
            "UPDATE session_settlements SET ended_at = ? WHERE session_id = 2", ((now - timedelta(days=7)).isoformat(),)
        )
        counts = retention.managed_recording_capacity(database)
    assert counts["recording_files"] == 2
    assert counts["recorded_bytes"] == sum(path.stat().st_size for path in original) + sum(
        (workspace.media_path(other[key])).stat().st_size for key in ("video", "comment_srt")
    )
    assert (
        retention.run_media_retention(workspace, retention.MediaRetentionPolicy(maximum_bytes=1))["state"] == "disabled"
    )
    policy = retention.MediaRetentionPolicy(enabled=True, maximum_bytes=1, minimum_free_bytes=0)

    outcome = retention.run_media_retention(workspace, policy, now=now)

    assert outcome["state"] == "completed"
    assert outcome["reason"] == "capacity_limit"
    assert outcome["operation"]["session_id"] == 2
    assert not other_video.exists()
    assert all(path.exists() for path in original)
    with closing(connect(workspace.database)) as database:
        assert database.execute("SELECT count(*) FROM stream_sessions").fetchone()[0] == 2
        assert database.execute("SELECT count(*) FROM comments").fetchone()[0] == 4


def test_age_retention_uses_confirmed_end_time_and_blocks_unsettled_media(tmp_path):
    workspace, outputs = collected(tmp_path)
    now = datetime.now(UTC)
    policy = retention.MediaRetentionPolicy(enabled=True, keep_days=1, minimum_free_bytes=0)

    assert retention.run_media_retention(workspace, policy, now=now)["state"] == "idle"
    with closing(connect(workspace.database)) as database, database:
        database.execute("UPDATE stream_sessions SET stream_state = 'interrupted'")
    assert retention.run_media_retention(workspace, policy, now=now + timedelta(days=2))["state"] == "idle"
    assert all(path.exists() for path in outputs)
    with closing(connect(workspace.database)) as database, database:
        database.execute("UPDATE stream_sessions SET stream_state = 'ended'")

    outcome = retention.run_media_retention(workspace, policy, now=now + timedelta(days=2))

    assert outcome["state"] == "completed"
    assert outcome["reason"] == "retention_period"
    assert all(not path.exists() for path in outputs)


def test_comment_expiration_recovers_capture_and_keeps_video_and_voice(tmp_path, monkeypatch):
    workspace, outputs = collected(tmp_path)
    video, comment_srt, speech_srt, ass = outputs
    real_unlink = retention._unlink_captured

    def interrupted(dir_fd):
        real_unlink(dir_fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(retention, "_unlink_captured", interrupted)
    with pytest.raises(KeyboardInterrupt):
        expire_fixture_comments(workspace, "tiktok")
    with closing(connect(workspace.database)) as database:
        assert database.execute("SELECT count(*) FROM comments").fetchone()[0] == 2
        assert database.execute("SELECT state FROM comment_expirations").fetchone()[0] == "running"
    monkeypatch.setattr(retention, "_unlink_captured", real_unlink)

    [result] = retention.resume_media_deletions(workspace)

    assert result["mode"] == "comments"
    assert result["state"] == "completed"
    assert not comment_srt.exists() and not ass.exists()
    assert video.is_file() and speech_srt.is_file()
    assert expire_fixture_comments(workspace, "tiktok")["removed_comments"] == 2


def test_automatic_comment_retention_uses_confirmed_end_and_zero_means_unlimited(tmp_path):
    workspace, outputs = collected(tmp_path)
    _, comment_srt, speech_srt, ass = outputs
    now = datetime.now(UTC) + timedelta(days=2)

    assert retention.run_comment_retention(workspace, 0, now=now)["state"] == "disabled"
    assert comment_srt.exists() and ass.exists()
    assert retention.run_comment_retention(workspace, 1, now=now)["state"] == "expired"
    assert not comment_srt.exists() and not ass.exists()
    assert speech_srt.exists()
    assert retention.run_comment_retention(workspace, 1, now=now)["state"] == "idle"


def test_comment_expiration_cleans_registered_sidecars_with_comment_data(tmp_path, monkeypatch):
    workspace, outputs = collected(tmp_path)
    _, comment_srt, speech_srt, ass = outputs
    monkeypatch.setattr(publication, "_cleanup_operation", lambda *_: None)
    with workspace.exclusive(), closing(connect(workspace.database)) as database:
        segment = database.execute("SELECT * FROM recording_segments").fetchone()
        publication.publish_subtitle_revision(database, workspace, segment, "ass", "replacement", 8)
        backup = workspace.media_path(
            database.execute("SELECT backup_path FROM subtitle_revision_operations WHERE kind = 'ass'").fetchone()[0]
        )
    assert backup.is_file()

    expire_fixture_comments(workspace, "tiktok")

    assert not backup.exists() and not comment_srt.exists() and not ass.exists()
    assert speech_srt.exists()
