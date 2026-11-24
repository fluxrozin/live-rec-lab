import json
import os
import shutil
import sqlite3
from contextlib import closing

import pytest

from live_rec_lab.cli import main
from live_rec_lab.collection import run_fixture
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.runtime import backup_fixture, diagnose_fixture


def test_fixture_diagnosis_does_not_initialize_or_modify_workspace(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    before = workspace.database.read_bytes()

    report = diagnose_fixture(workspace)

    assert report["mode"] == "fixture"
    assert report["database"]["integrity"] == "ok"
    assert report["database"]["sessions"] == 0
    assert "ffmpeg version" in report["tools"]["ffmpeg"]
    assert "ffprobe version" in report["tools"]["ffprobe"]
    assert json.dumps(report)
    assert workspace.database.read_bytes() == before
    assert not workspace.events.exists()
    assert not workspace.lock.exists()


def test_fixture_backup_keeps_a_consistent_database_snapshot(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)

    first = backup_fixture(workspace)
    second = backup_fixture(workspace)

    assert first.parent == workspace.root / "state" / "backups"
    assert first != second
    for snapshot in (first, second):
        assert snapshot.stat().st_mode & 0o777 == 0o600
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as restore_candidate:
            assert restore_candidate.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restore_candidate.execute("SELECT count(*) FROM comments").fetchone() == (2,)


def test_expiration_removes_comment_subtitles_first_and_cannot_recreate_ass(tmp_path):
    import pytest

    from live_rec_lab.maintenance import expire_fixture_comments
    from live_rec_lab.postprocess import postprocess_fixture

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    collection = run_fixture(workspace)
    subtitles = postprocess_fixture(workspace, "tiktok")
    result = expire_fixture_comments(workspace, "tiktok")

    assert result == {"mode": "fixture", "state": "expired", "removed_comments": 2}
    assert not (workspace.root / collection["comment_srt"]).exists()
    assert not (workspace.root / subtitles["ass"]).exists()
    assert (workspace.root / collection["video"]).is_file()
    assert (workspace.root / subtitles["speech_srt"]).is_file()
    assert expire_fixture_comments(workspace, "tiktok") == result
    assert run_fixture(workspace)["comment_count"] is None
    with pytest.raises(ValueError, match="expired"):
        postprocess_fixture(workspace, "tiktok")


def test_expiration_holds_if_managed_ass_has_been_replaced(tmp_path):
    import pytest

    from live_rec_lab.maintenance import expire_fixture_comments
    from live_rec_lab.postprocess import postprocess_fixture

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    collection = run_fixture(workspace)
    subtitles = postprocess_fixture(workspace, "tiktok")
    replacement = workspace.root / subtitles["ass"]
    replacement.write_bytes(b"unmanaged replacement")

    with pytest.raises(ValueError, match="generation"):
        expire_fixture_comments(workspace, "tiktok")

    assert (workspace.root / collection["comment_srt"]).is_file()
    assert replacement.read_bytes() == b"unmanaged replacement"
    with closing(sqlite3.connect(workspace.database)) as database:
        assert database.execute("SELECT count(*) FROM comments").fetchone() == (2,)


def test_restore_check_reads_a_consistent_fixture_backup_without_changing_database(tmp_path):
    from live_rec_lab.maintenance import inspect_fixture_restore

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)
    snapshot = backup_fixture(workspace)
    before = workspace.database.read_bytes()
    report = inspect_fixture_restore(workspace, snapshot.name)

    assert report["state"] == "reviewable"
    assert report["issues"] == {}
    assert workspace.database.read_bytes() == before
    assert not (workspace.events.parent / "restore-in-progress.json").exists()


def test_restore_check_blocks_expired_comments_and_missing_current_media(tmp_path):
    from live_rec_lab.maintenance import expire_fixture_comments, inspect_fixture_restore
    from live_rec_lab.postprocess import postprocess_fixture

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)
    postprocess_fixture(workspace, "tiktok")
    older = backup_fixture(workspace)
    expire_fixture_comments(workspace, "tiktok")
    run_fixture(workspace, "twitcasting")

    report = inspect_fixture_restore(workspace, older.name)

    assert report["state"] == "blocked"
    assert report["issues"]["expired_comments_in_backup"] == 2
    assert report["issues"]["expired_comment_subtitles_in_backup"] == 2
    assert report["issues"]["newer_recordings_missing_from_backup"] == 1


def test_restore_check_wont_open_unmanaged_snapshot(tmp_path):
    import pytest

    from live_rec_lab.maintenance import inspect_fixture_restore

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    snapshot = backup_fixture(workspace)
    sibling = snapshot.parent / "fixture-20261124T000000Z-abcdefabcdef.sqlite3"
    sibling.symlink_to(snapshot)

    with pytest.raises(ValueError, match="backup"):
        inspect_fixture_restore(workspace, sibling.name)
    with pytest.raises(ValueError, match="backup"):
        inspect_fixture_restore(workspace, "../" + snapshot.name)


def _install_test_snapshot(workspace, snapshot):
    staged = workspace.database.with_suffix(".restore")
    shutil.copyfile(snapshot, staged)
    staged.chmod(0o600)
    os.replace(staged, workspace.database)


def test_manual_fixture_restore_fences_server_and_jobs_until_verified(tmp_path, capsys):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)
    candidate = backup_fixture(workspace)
    root = str(workspace.root)
    marker = workspace.events.parent / "restore-in-progress.json"

    assert main(["fixture", "config", root, "set", "web_port", "18083"]) == 0
    capsys.readouterr()
    assert diagnose_fixture(workspace)["database"]["integrity"] == "ok"
    with workspace.exclusive(worker="speech"):
        assert main(["fixture", "restore-prepare", root, candidate.name]) == 1
    assert not marker.exists()
    assert main(["fixture", "restore-prepare", root, candidate.name]) == 0
    prepared = json.loads(capsys.readouterr().out)
    rollback = candidate.parent / prepared["rollback"]
    assert marker.is_file() and rollback.is_file() and rollback != candidate
    assert diagnose_fixture(workspace)["runtime"]["state"] == "restore_in_progress"
    assert main(["fixture", "serve", root]) == 1
    assert main(["fixture", "config", root, "set", "web_port", "8084"]) == 1
    with pytest.raises(ValueError, match="restore"):
        run_fixture(workspace)

    _install_test_snapshot(workspace, candidate)
    assert main(["fixture", "restore-finish", root]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "restored"
    assert not marker.exists()
    assert main(["fixture", "config", root, "get", "web_port"]) == 0
    assert json.loads(capsys.readouterr().out)["web_port"] == 8083
    assert run_fixture(workspace)["comment_count"] == 2


def test_manual_fixture_restore_preserves_marker_for_unexpected_database_and_allows_rollback(tmp_path, capsys):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    candidate = backup_fixture(workspace)
    root = str(workspace.root)
    marker = workspace.events.parent / "restore-in-progress.json"

    assert main(["fixture", "config", root, "set", "web_port", "18083"]) == 0
    capsys.readouterr()
    assert main(["fixture", "restore-prepare", root, candidate.name]) == 0
    rollback = candidate.parent / json.loads(capsys.readouterr().out)["rollback"]
    with closing(sqlite3.connect(workspace.database)) as database, database:
        database.execute("CREATE TABLE unverified (id INTEGER)")
    assert main(["fixture", "restore-finish", root]) == 1
    assert marker.is_file()
    _install_test_snapshot(workspace, rollback)
    sidecar = workspace.database.with_name(workspace.database.name + "-wal")
    sidecar.touch()
    assert main(["fixture", "restore-finish", root]) == 1
    assert sidecar.exists() and marker.is_file()
    sidecar.unlink()

    assert main(["fixture", "restore-finish", root]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "rolled_back"
    assert not marker.exists()
    assert main(["fixture", "config", root, "get", "web_port"]) == 0
    assert json.loads(capsys.readouterr().out)["web_port"] == 18083


def test_restore_prepare_rejects_an_older_speech_queue(tmp_path, capsys):
    from live_rec_lab.speech_jobs import enqueue_speech

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)
    older = backup_fixture(workspace)
    enqueue_speech(workspace, 1)

    assert main(["fixture", "restore-prepare", str(workspace.root), older.name]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "blocked"
    assert report["issues"]["changed_speech_attempts"]
    assert not (workspace.events.parent / "restore-in-progress.json").exists()
