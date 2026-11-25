"""インスタンス排他・固定ポート・endpoint.json（要求§2.2、§15.2.1）。"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from live_rec_lab.config import ConfigError, Environment, ensure_private_dir


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class InstanceLock:
    """環境ごとの排他ロック。保持中は他プロセスが同一環境を起動できない。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise ConfigError("同じ環境の別インスタンスが稼働中です（排他ロック取得失敗）") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def _listener_present(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def bind_port(port: int, *, wait_s: float = 150.0) -> socket.socket:
    """固定ポートへ直接バインドして確保済みソケットを返す。自動変更はしない。

    WSL2のmirroredモードではWindows側のTIME_WAITが再バインドを最大約2分ブロックする（実測40秒〜）。
    別プロセスが実際にlistenしている場合は即拒否し、listener不在のEADDRINUSEだけ同じポートで待つ。
    """
    deadline = time.monotonic() + wait_s
    announced = False
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            break
        except OSError as exc:
            sock.close()
            if exc.errno != errno.EADDRINUSE or _listener_present(port) or time.monotonic() >= deadline:
                raise ConfigError(f"ポート {port} をバインドできません（使用中の可能性）: {exc}") from exc
            if not announced:
                print(f"ポート {port} は前回接続のTIME_WAIT等で一時的に使用中です。同じポートで待機します…", flush=True)
                announced = True
            time.sleep(1.0)
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


@dataclass
class Endpoint:
    url: str
    port: int
    pid: int
    started_at: str
    instance_id: str
    mode: str

    def write(self, env: Environment) -> None:
        ensure_private_dir(env.runtime_dir)
        target = env.runtime_dir / "endpoint.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)


def new_instance_id() -> str:
    return uuid.uuid4().hex[:16]


def _pid_alive(pid: int, started_at: str | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_endpoint(env: Environment) -> dict | None:
    """生存確認済みの endpoint のみ返す（古い情報は None）。"""
    f = env.runtime_dir / "endpoint.json"
    try:
        data = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _pid_alive(int(data.get("pid", 0))):
        return None
    # PID再利用対策: ロックが保持されていなければ稼働していない
    lock = env.runtime_dir / "instance.lock"
    if lock.exists() and not lock_held(lock):
        return None
    return data


def lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def remove_endpoint(env: Environment, instance_id: str) -> None:
    f = env.runtime_dir / "endpoint.json"
    try:
        if json.loads(f.read_text("utf-8")).get("instance_id") == instance_id:
            f.unlink()
    except (OSError, ValueError):
        pass


def wait_until(predicate, timeout: float, interval: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
