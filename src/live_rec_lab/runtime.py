"""Operational commands scoped to a verified synthetic workspace."""

import os
import socket
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .isolation import FixtureWorkspace, check_fixture_directory
from .media import media_tool, publish_exclusive


def diagnose_fixture(workspace: FixtureWorkspace) -> dict[str, object]:
    workspace = FixtureWorkspace.open(workspace.root)
    with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
        status = database.execute("PRAGMA quick_check").fetchone()[0]
        database_state = {
            "integrity": status,
            "schema_version": database.execute("PRAGMA user_version").fetchone()[0],
            "sessions": database.execute("SELECT count(*) FROM stream_sessions").fetchone()[0],
        }
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        try:
            tools[name] = media_tool(name, ["-version"]).splitlines()[0]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            tools[name] = {"state": "unavailable", "reason": str(error)}
    return {
        "mode": "fixture",
        "database": database_state,
        "tools": tools,
        "external_connections": "blocked",
        "as_of": datetime.now(UTC).isoformat(),
    }


def backup_fixture(workspace: FixtureWorkspace) -> Path:
    with workspace.exclusive():
        workspace = FixtureWorkspace.open(workspace.root)
        backups = workspace.events.parent / "backups"
        backups.mkdir(mode=0o700, exist_ok=True)
        check_fixture_directory(backups, private=True)
        filename = f"fixture-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}.sqlite3"
        destination = backups / filename
        with tempfile.NamedTemporaryFile(dir=backups, suffix=".part.sqlite3", delete=False) as staging:
            temporary = Path(staging.name)
        with (
            closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(temporary)) as snapshot,
        ):
            source.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("fixture backup did not pass integrity verification")
        with temporary.open("rb") as completed:
            os.fsync(completed.fileno())
        publish_exclusive(temporary, destination)
        workspace.event("backup", "completed", filename)
        return destination


def serve_fixture(workspace: FixtureWorkspace, port: int) -> None:
    import uvicorn

    from .web.app import create_fixture_app

    with workspace.exclusive(worker="server"), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        app = create_fixture_app(workspace, port)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        print(f"http://127.0.0.1:{port}/", flush=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, proxy_headers=False, access_log=False)
        uvicorn.Server(config).run(sockets=[listener])
