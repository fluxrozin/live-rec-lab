"""Freeze an isolated workspace around an operator's manual SQLite replacement."""

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from .db import connect
from .isolation import FixtureWorkspace
from .maintenance import check_fixture_restore, fixture_backup_path
from .media import generation, publish_exclusive
from .runtime import snapshot_fixture_database


@contextmanager
def _stopped_workspace(workspace: FixtureWorkspace) -> Iterator[None]:
    with ExitStack() as locks:
        for worker in ("server", "speech", "comments", None):
            locks.enter_context(workspace.exclusive(worker=worker, maintenance=True))
        yield


def _require_standalone(path: Path) -> None:
    if any(os.path.lexists(path.with_name(path.name + suffix)) for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("SQLite sidecar is present during restore")


def _checkpoint_current(path: Path) -> None:
    if os.path.lexists(path.with_name(path.name + "-journal")):
        raise ValueError("SQLite rollback journal is present during restore")
    if any(os.path.lexists(path.with_name(path.name + suffix)) for suffix in ("-wal", "-shm")):
        with closing(connect(path)) as database:
            busy, _, _ = database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise ValueError("SQLite WAL is still in use")
    _require_standalone(path)


def _digest(path: Path) -> str:
    _require_standalone(path)
    before = generation(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    _require_standalone(path)
    if generation(path) != before:
        raise ValueError("restore database changed while being verified")
    return digest


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_fixture_restore(workspace: FixtureWorkspace, snapshot: str) -> dict[str, object]:
    with _stopped_workspace(workspace):
        workspace.require_ready()
        candidate = fixture_backup_path(workspace, snapshot)
        candidate_digest = _digest(candidate)
        _checkpoint_current(workspace.database)
        issues = check_fixture_restore(workspace, workspace.database, candidate, immutable_current=True)
        if _digest(candidate) != candidate_digest:
            raise ValueError("restore candidate changed during preflight")
        if issues:
            return {"mode": "fixture", "snapshot": snapshot, "state": "blocked", "issues": issues}

        rollback = snapshot_fixture_database(workspace)
        _require_standalone(workspace.database)
        document = {
            "version": 1,
            "root": str(workspace.root),
            "snapshot": snapshot,
            "snapshot_sha256": candidate_digest,
            "rollback": rollback.name,
            "rollback_sha256": _digest(rollback),
        }
        _sync_directory(rollback.parent)
        marker = workspace.restore_marker
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=marker.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
                publish_exclusive(temporary, marker)
                _sync_directory(marker.parent)
            finally:
                temporary.unlink(missing_ok=True)
        workspace.event("restore", "prepared", rollback.name)
        return {"mode": "fixture", "snapshot": snapshot, "rollback": rollback.name, "state": "prepared"}


def finish_fixture_restore(workspace: FixtureWorkspace) -> dict[str, object]:
    with _stopped_workspace(workspace):
        marker = workspace.restore_marker
        marker_generation = generation(marker)
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError("restore marker permissions are unsafe")
            document = json.loads(stream.read(4097))
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or document.get("root") != str(workspace.root)
            or not all(
                isinstance(document.get(key), str)
                for key in ("snapshot", "rollback", "snapshot_sha256", "rollback_sha256")
            )
        ):
            raise ValueError("restore marker is unreadable")
        rollback = fixture_backup_path(workspace, document["rollback"])
        if _digest(rollback) != document["rollback_sha256"]:
            raise ValueError("rollback backup changed after restore preparation")
        installed = _digest(workspace.database)
        if installed == document["snapshot_sha256"]:
            state = "restored"
        elif installed == document["rollback_sha256"]:
            state = "rolled_back"
        else:
            raise ValueError("installed database differs from the prepared snapshot and rollback")

        issues = check_fixture_restore(workspace, rollback, workspace.database, immutable_current=True)
        if issues:
            return {"mode": "fixture", "state": "blocked", "issues": issues}
        if _digest(workspace.database) != installed or generation(marker) != marker_generation:
            raise ValueError("restore files changed during verification")
        with workspace.database.open("rb") as database:
            os.fsync(database.fileno())
        _sync_directory(workspace.database.parent)
        workspace.event("restore", state, rollback.name)
        marker.unlink()
        _sync_directory(marker.parent)
        return {"mode": "fixture", "state": state}
