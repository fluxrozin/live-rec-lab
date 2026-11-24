"""Verified endpoint discovery and a local, peer-checked shutdown channel."""

import hashlib
import json
import os
import re
import socket
import stat
import struct
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .isolation import FixtureWorkspace, check_fixture_directory
from .web_port import read_web_port, validate_web_port

_BOOT = Path("/proc/sys/kernel/random/boot_id")
_CREDENTIALS = struct.Struct("3i")
_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")


class EndpointUnconfirmed(ValueError):
    """Runtime information cannot be safely tied to the current app process."""


def _start_ticks(pid: int) -> int:
    process = Path("/proc") / str(pid)
    if process.stat().st_uid != os.geteuid():
        raise EndpointUnconfirmed("endpoint process belongs to another user")
    content = (process / "stat").read_text(encoding="utf-8", errors="replace")
    fields = content[content.rindex(")") + 2 :].split()
    tick = int(fields[19])
    if fields[0] in {"Z", "X", "x"} or tick < 0:
        raise EndpointUnconfirmed("endpoint process has exited")
    return tick


@dataclass(frozen=True)
class Endpoint:
    pid: int
    port: int
    started_at: str
    instance_id: str
    boot_id: str
    start_ticks: int

    @classmethod
    def current(cls, port: int) -> "Endpoint":
        pid = os.getpid()
        return cls(
            pid,
            validate_web_port(port),
            datetime.now(UTC).isoformat(),
            uuid4().hex,
            _BOOT.read_text().strip(),
            _start_ticks(pid),
        )

    def address(self, workspace: FixtureWorkspace) -> str:
        scope = hashlib.sha256(os.fsencode(workspace.root)).hexdigest()[:32]
        return f"\0live-rec-lab-{scope}-{self.instance_id}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def document(self) -> dict[str, object]:
        return {**asdict(self), "url": self.url, "mode": "fixture"}


def endpoint_path(workspace: FixtureWorkspace) -> Path:
    return workspace.events.parent / "runtime" / "endpoint.json"


def _read_document(workspace: FixtureWorkspace) -> Endpoint | None:
    path = endpoint_path(workspace)
    try:
        check_fixture_directory(path.parent, private=True)
        file = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise EndpointUnconfirmed("runtime endpoint is unreadable") from error
    try:
        with os.fdopen(file, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and info.st_mode & 0o077 == 0
                and 0 < info.st_size <= 4096
            ):
                raise ValueError("unsafe endpoint file")
            raw = stream.read(4097)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("mode") != "fixture":
            raise ValueError("unexpected endpoint mode")
        endpoint = Endpoint(
            data["pid"],
            validate_web_port(data["port"]),
            data["started_at"],
            data["instance_id"],
            data["boot_id"],
            data["start_ticks"],
        )
        if (
            type(endpoint.pid) is not int
            or endpoint.pid <= 1
            or type(endpoint.start_ticks) is not int
            or endpoint.start_ticks < 0
            or _IDENTIFIER.fullmatch(endpoint.instance_id) is None
            or datetime.fromisoformat(endpoint.started_at).utcoffset() is None
            or endpoint.url != data.get("url")
        ):
            raise ValueError("invalid endpoint identity")
        return endpoint
    except (IndexError, KeyError, TypeError, ValueError, OSError) as error:
        raise EndpointUnconfirmed("runtime endpoint identity is unconfirmed") from error


def assert_endpoint_replaceable(workspace: FixtureWorkspace) -> None:
    # A regular file left by the previously owned instance can be replaced under the server lock.
    path = endpoint_path(workspace)
    try:
        check_fixture_directory(path.parent, private=True)
        previous = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(previous.st_mode)
        or previous.st_uid != os.geteuid()
        or previous.st_nlink != 1
        or previous.st_mode & 0o077
    ):
        raise EndpointUnconfirmed("runtime endpoint path is not owned")


def publish_endpoint(workspace: FixtureWorkspace, endpoint: Endpoint) -> None:
    assert_endpoint_replaceable(workspace)
    path = endpoint_path(workspace)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    check_fixture_directory(path.parent, private=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(endpoint.document(), stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def remove_endpoint(workspace: FixtureWorkspace, endpoint: Endpoint) -> None:
    try:
        current = _read_document(workspace)
        if current == endpoint:
            endpoint_path(workspace).unlink(missing_ok=True)
    except EndpointUnconfirmed as error:
        workspace.event("runtime", "endpoint_cleanup_preserved", endpoint.instance_id, error)


def peer_credentials(channel: socket.socket) -> tuple[int, int, int]:
    return _CREDENTIALS.unpack(channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDENTIALS.size))


def _request(workspace: FixtureWorkspace, endpoint: Endpoint, command: str) -> None:
    try:
        if _BOOT.read_text().strip() != endpoint.boot_id or _start_ticks(endpoint.pid) != endpoint.start_ticks:
            raise EndpointUnconfirmed("runtime process identity changed")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(1.0)
            channel.connect(endpoint.address(workspace))
            pid, uid, _ = peer_credentials(channel)
            if pid != endpoint.pid or uid != os.geteuid():
                raise EndpointUnconfirmed("runtime control channel belongs to another process")
            channel.sendall(f"{command} {endpoint.instance_id}\n".encode("ascii"))
            with channel.makefile("rb") as response:
                if response.readline(50) != f"ok {endpoint.instance_id}\n".encode("ascii"):
                    raise EndpointUnconfirmed("runtime control channel did not confirm identity")
    except (IndexError, ValueError, OSError) as error:
        raise EndpointUnconfirmed("running fixture server could not be verified") from error


def fixture_status(workspace: FixtureWorkspace) -> dict[str, object]:
    workspace = FixtureWorkspace.open(workspace.root)
    if workspace.restore_pending:
        return {"mode": "fixture", "state": "restore_in_progress"}
    saved = read_web_port(workspace)
    baseline = {"mode": "fixture", "configured_port": saved.web_port, "port_revision": saved.revision}
    try:
        endpoint = _read_document(workspace)
        if endpoint is None:
            return {**baseline, "state": "stopped"}
        _request(workspace, endpoint, "status")
    except EndpointUnconfirmed:
        return {**baseline, "state": "unconfirmed"}
    return {
        **endpoint.document(),
        **baseline,
        "state": "running",
        "restart_required": saved.web_port != endpoint.port,
    }


def stop_fixture(workspace: FixtureWorkspace, *, timeout: float = 30) -> dict[str, object]:
    workspace = FixtureWorkspace.open(workspace.root)
    workspace.require_ready()
    endpoint = _read_document(workspace)
    if endpoint is None:
        return {"mode": "fixture", "state": "stopped"}
    _request(workspace, endpoint, "stop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _read_document(workspace)
        if current is None or current.instance_id != endpoint.instance_id:
            return {"mode": "fixture", "state": "stopped", "instance_id": endpoint.instance_id}
        time.sleep(0.05)
    raise EndpointUnconfirmed("fixture server has not confirmed shutdown")
