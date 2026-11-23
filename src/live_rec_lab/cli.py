"""起動、状態確認、安全停止、手動 SQLite バックアップ。"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

import uvicorn

from live_rec_lab.config import load_paths
from live_rec_lab.db.queries import load_settings
from live_rec_lab.db.session import session_scope, upgrade_database
from live_rec_lab.ops.backup import backup_database
from live_rec_lab.ops.logging_config import redact

_LOOPBACK_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")
_MAX_ENDPOINT_BYTES = 8192


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _process_start_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rpartition(")")[2].split()
    if fields[0] in {"X", "Z"}:
        raise ProcessLookupError(pid)
    return int(fields[19])


def _endpoint_record(path: Path) -> dict | None:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as endpoint:
            info = os.fstat(endpoint.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                return None
            if info.st_size > _MAX_ENDPOINT_BYTES:
                return None
            value = json.loads(endpoint.read(_MAX_ENDPOINT_BYTES))
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _matches_process(payload: dict) -> bool:
    pid, start_ticks = payload.get("pid"), payload.get("start_ticks")
    if type(pid) is not int or pid < 1 or type(start_ticks) is not int or start_ticks < 1:
        return False
    try:
        return payload.get("boot_id") == _boot_id() and start_ticks == _process_start_ticks(pid)
    except (OSError, ValueError, IndexError):
        return False


def read_endpoint(path: Path) -> dict | None:
    payload = _endpoint_record(path)
    if payload is None or not _matches_process(payload):
        return None
    url = payload.get("url")
    port_match = _LOOPBACK_URL.fullmatch(url) if isinstance(url, str) else None
    if not port_match or not 1 <= int(port_match[1]) <= 65535:
        return None
    try:
        UUID(payload["instance_id"])
        if datetime.fromisoformat(payload["started_at"]).utcoffset() is None:
            return None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None

    connection = http.client.HTTPConnection("127.0.0.1", int(port_match[1]), timeout=0.5)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        health = json.loads(response.read(_MAX_ENDPOINT_BYTES))
        if (
            response.status == 200
            and isinstance(health, dict)
            and health.get("status") == "ok"
            and health.get("pid") == payload["pid"]
            and health.get("instance_id") == payload["instance_id"]
        ):
            return payload
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    return None


def _lock(path: Path) -> BinaryIO | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    handle = os.fdopen(fd, "rb+")
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError(f"実行時ロックのファイルを確認できません: {path}")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except BaseException:
        handle.close()
        raise
    return handle


def _write_endpoint(path: Path, payload: dict) -> None:
    descriptor, filename = tempfile.mkstemp(dir=path.parent, prefix=".endpoint-", suffix=".tmp")
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _EndpointServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, endpoint: Path, payload: dict) -> None:
        super().__init__(config)
        self.endpoint = endpoint
        self.payload = payload
        self.published = False

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.started:
            return
        try:
            _write_endpoint(self.endpoint, self.payload)
        except OSError as exc:
            print(f"起動情報を記録できません: {redact(str(exc))}", file=sys.stderr)
            self.should_exit = True
            return
        self.published = True
        print(self.payload["url"], flush=True)

    def clear_endpoint(self) -> None:
        recorded = _endpoint_record(self.endpoint)
        if recorded is not None and recorded.get("instance_id") == self.payload["instance_id"]:
            self.endpoint.unlink(missing_ok=True)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().shutdown(sockets=sockets)
        finally:
            # Uvicorn replays SIGTERM after shutdown, before Server.run returns.
            self.clear_endpoint()


def _start() -> int:
    paths = load_paths()
    with ExitStack() as lifetime:
        saved_mask = os.umask(0o077)
        lifetime.callback(os.umask, saved_mask)
        paths.ensure()
        for lock_path in (paths.data_dir / "instance.lock", paths.lock_path):
            handle = _lock(lock_path)
            if handle is None:
                print("同じ SQLite または実行時ディレクトリで既に起動しています", file=sys.stderr)
                return 1
            lifetime.enter_context(handle)

        upgrade_database()
        with session_scope() as db:
            port = int(load_settings(db)["web_port"])
        sock = lifetime.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(2048)
        assigned = sock.getsockname()[1]
        os.environ["LIVE_REC_LAB_BOUND_PORT"] = str(assigned)
        from live_rec_lab.app import create_app

        app = create_app()
        coordinator = app.state.coordinator
        pid = os.getpid()
        payload = {
            "url": f"http://127.0.0.1:{assigned}",
            "pid": pid,
            "started_at": coordinator.started_at.isoformat(),
            "instance_id": coordinator.instance_id,
            "boot_id": _boot_id(),
            "start_ticks": _process_start_ticks(pid),
        }
        config = uvicorn.Config(
            app, host="127.0.0.1", port=assigned, workers=1, reload=False, lifespan="on",
            log_level="info", access_log=False, proxy_headers=False, timeout_graceful_shutdown=30,
        )
        server = _EndpointServer(config, paths.endpoint_path, payload)
        try:
            server.run(sockets=[sock])
        finally:
            server.clear_endpoint()
        return 0 if server.published else 1


def _status() -> int:
    payload = read_endpoint(load_paths().endpoint_path)
    if payload is None:
        print("稼働を確認できません")
        return 1
    print(f"稼働中 {payload['url']} pid={payload['pid']} instance={payload['instance_id']}")
    return 0


def _url() -> int:
    payload = read_endpoint(load_paths().endpoint_path)
    if payload is None:
        print("起動URLを確認できません", file=sys.stderr)
        return 1
    print(payload["url"])
    return 0


def _backup(dest: str) -> int:
    snapshot = Path(dest).expanduser().absolute()
    print(f"SQLite をバックアップします: {snapshot}。コメント本文・投稿者情報を含む可能性があります。", file=sys.stderr)
    output = backup_database(snapshot)
    print(output)
    print(
        f"{output} は保存期限による自動削除の対象外です。古いバックアップは安全に破棄または差し替えてください。"
        "手動復元後は運用再開前にコメント期限と管理下のSRTを再照合してください。",
        file=sys.stderr,
    )
    return 0


def _stop() -> int:
    payload = read_endpoint(load_paths().endpoint_path)
    if payload is None:
        print("停止対象のインスタンスを確認できません", file=sys.stderr)
        return 1
    pidfd = os.pidfd_open(payload["pid"])
    try:
        if not _matches_process(payload):
            raise ProcessLookupError("停止対象のプロセスが入れ替わりました")
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    finally:
        os.close(pidfd)
    print("停止を要求しました")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="live-rec-lab")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("url")
    backup = sub.add_parser("backup")
    backup.add_argument("dest")
    sub.add_parser("stop")
    args = parser.parse_args(argv)
    commands = {
        "start": _start,
        "status": _status,
        "url": _url,
        "backup": lambda: _backup(args.dest),
        "stop": _stop,
    }
    try:
        status = commands[args.command]()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(redact(str(exc)), file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()
