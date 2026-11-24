"""Operational commands scoped to a verified synthetic workspace."""

import asyncio
import os
import socket
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .endpoint import (
    Endpoint,
    assert_endpoint_replaceable,
    fixture_status,
    peer_credentials,
    publish_endpoint,
    remove_endpoint,
)
from .isolation import FixtureWorkspace, Workspace
from .media import media_tool, publish_exclusive
from .web_port import read_web_port, validate_web_port


def diagnose_fixture(workspace: Workspace) -> dict[str, object]:
    workspace = workspace.recheck()
    database_state: dict[str, object] = {"integrity": "unconfirmed", "reason": "restore_in_progress"}
    if not workspace.restore_pending:
        with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
            database_state = {
                "integrity": database.execute("PRAGMA quick_check").fetchone()[0],
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
        "mode": workspace.mode,
        "database": database_state,
        "tools": tools,
        "runtime": fixture_status(workspace),
        "external_connections": "blocked",
        "as_of": datetime.now(UTC).isoformat(),
    }


def backup_fixture(workspace: Workspace) -> Path:
    with workspace.exclusive():
        return snapshot_fixture_database(workspace)


def snapshot_fixture_database(workspace: Workspace, destination: Path | None = None) -> Path:
    """Save an Online Backup while the caller holds the workspace writer lock."""
    workspace = workspace.recheck()
    backups = workspace.backup_path if destination is None else destination.parent
    if destination is None:
        backups.mkdir(mode=0o700, exist_ok=True)
    workspace.check_directory(backups, private=destination is None)
    filename = f"fixture-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}.sqlite3"
    destination = backups / filename if destination is None else destination
    with tempfile.NamedTemporaryFile(dir=backups, suffix=".part.sqlite3", delete=False) as staging:
        temporary = Path(staging.name)
    try:
        with (
            closing(sqlite3.connect(workspace.database.as_uri() + "?mode=rw", uri=True)) as source,
            closing(sqlite3.connect(temporary)) as snapshot,
        ):
            source.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("fixture backup did not pass integrity verification")
        with temporary.open("rb") as completed:
            os.fsync(completed.fileno())
        publish_exclusive(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    workspace.event("backup", "completed", filename)
    return destination


def serve_fixture(workspace: FixtureWorkspace, port: int | None = None) -> None:
    from .workers import RuntimeWorkers

    serve_workspace(workspace, port, workers=RuntimeWorkers(workspace))


def serve_workspace(workspace: Workspace, port: int | None = None, *, workers=None) -> None:
    import uvicorn

    from .web.app import create_app

    with workspace.exclusive(worker="server"), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        port = read_web_port(workspace).web_port if port is None else validate_web_port(port)
        assert_endpoint_replaceable(workspace)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen()
        except OSError as error:
            raise OSError(f"could not bind fixture port {port}: {error}") from error

        endpoint = Endpoint.current(port, workspace.mode)
        app = create_app(workspace, port)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            workers=1,
            proxy_headers=False,
            access_log=False,
            timeout_graceful_shutdown=20,
        )

        class FixtureServer(uvicorn.Server):
            control: asyncio.Server | None = None

            async def startup(self, sockets=None):
                await super().startup(sockets)
                if not self.started or self.should_exit:
                    return
                self.control = await asyncio.start_unix_server(
                    self.handle_control, path=endpoint.address(workspace), limit=128
                )
                publish_endpoint(workspace, endpoint)
                print(endpoint.url, flush=True)
                if workers is not None:
                    workers.start()

            async def handle_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    _, uid, _ = peer_credentials(writer.get_extra_info("socket"))
                    if uid != os.geteuid():
                        return
                    identity = endpoint.instance_id.encode("ascii") + b"\n"
                    command = await asyncio.wait_for(reader.readline(), timeout=1)
                    if command in {b"status " + identity, b"stop " + identity}:
                        writer.write(b"ok " + identity)
                        await writer.drain()
                        if command == b"stop " + identity:
                            self.should_exit = True
                except (OSError, ValueError, TimeoutError):
                    return
                finally:
                    writer.close()

            async def shutdown(self, sockets=None):
                if workers is not None:
                    workers.signal_stop()
                if self.control is not None:
                    self.control.close()
                    await self.control.wait_closed()
                await super().shutdown(sockets)

        try:
            FixtureServer(config).run(sockets=[listener])
        finally:
            if workers is not None:
                workers.stop()
            remove_endpoint(workspace, endpoint)
