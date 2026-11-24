import json
from pathlib import Path

import pytest

from live_rec_lab.isolation import FixtureWorkspace, IsolationError


def test_workspace_is_new_private_and_reopenable(tmp_path):
    root = tmp_path / "fixture"

    created = FixtureWorkspace.initialize(root)

    assert FixtureWorkspace.open(root) == created
    assert root.stat().st_mode & 0o777 == 0o700
    assert created.recordings.is_dir()
    assert created.database.parent.is_dir()

    with pytest.raises(FileExistsError):
        FixtureWorkspace.initialize(root)


def test_workspace_rejects_root_outside_tmp():
    with pytest.raises(IsolationError, match="/tmp"):
        FixtureWorkspace.open(Path("/mnt/d/LiveRecorder"))


def test_workspace_rejects_symlink_parent(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    with pytest.raises(IsolationError, match="link"):
        FixtureWorkspace.initialize(alias / "fixture")

    assert not (actual / "fixture").exists()


def test_workspace_rejects_repointed_database(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    unmanaged = tmp_path / "other.sqlite3"
    unmanaged.write_bytes(b"unmanaged")
    workspace.database.unlink()
    workspace.database.symlink_to(unmanaged)

    with pytest.raises(IsolationError, match="file"):
        FixtureWorkspace.open(workspace.root)

    assert unmanaged.read_bytes() == b"unmanaged"


def test_workspace_lock_refuses_a_second_owner(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")

    with workspace.exclusive():
        with pytest.raises(IsolationError, match="busy"), workspace.exclusive():
            pytest.fail("a second owner entered the workspace")


def test_workspace_log_is_structured(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")

    workspace.event("recorder", "failed", "fixture-session", ValueError("media invalid"))

    event = json.loads(workspace.events.read_text())
    assert event["timestamp"].endswith("+00:00")
    assert (event["component"], event["event"], event["correlation_id"]) == ("recorder", "failed", "fixture-session")
    assert event["exception"] == {"type": "ValueError", "message": "media invalid"}


def test_directly_constructed_workspace_cannot_write_without_marker(tmp_path):
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir(mode=0o700)
    state = unmarked / "state"
    state.mkdir(mode=0o700)
    workspace = FixtureWorkspace(unmarked)

    with pytest.raises(FileNotFoundError), workspace.exclusive():
        pytest.fail("unverified workspace acquired a writer lock")
    with pytest.raises(FileNotFoundError):
        workspace.event("test", "unverified", "fixture")

    assert list(state.iterdir()) == []
