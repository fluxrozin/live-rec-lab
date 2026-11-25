"""外部通信の既定拒否・共通排他・永続予算（要求§15.2.1）。

* 既定は全外部通信を拒否。許可は隔離の ``--allow-live``（永続化しない）か通常運用の有効化済み対象のみ。
* 実接続は共通のロック（SHARED_DIR/external.lock）を接続前から終了まで保持する。
* 予算はアプリDBと独立した小さなSQLite。通信前に安全側に計上し、再起動・DB復元で初期化しない。
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from live_rec_lab.config import SHARED_DIR, ConfigError, ensure_private_dir

LOCK_FILE = SHARED_DIR / "external.lock"
BUDGET_FILE = SHARED_DIR / "external-budget.sqlite3"

# サービス別の許可ホスト（接尾辞一致）。ここに無い宛先は許可済みでも拒否する。
ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "tiktok": ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokv.com", "byteoversea.com", "ibytedtos.com"),
    "eulerstream": ("eulerstream.com",),
    "twitcasting": ("twitcasting.tv", "twitcasting.jp", "twimg.com"),
    "loopback": ("127.0.0.1", "localhost"),
}


class ExternalDenied(RuntimeError):
    pass


def host_allowed(service: str, host: str) -> bool:
    host = host.lower().rstrip(".")
    for suffix in ALLOWED_HOSTS.get(service, ()):
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


class Budget:
    """永続予算。key単位に窓内の計上数と待機期限を保持する。"""

    def __init__(self, path: Path = BUDGET_FILE) -> None:
        self.path = path
        ensure_private_dir(path.parent)
        with self._con() as con:
            con.execute("create table if not exists attempts (key text not null, attempted real not null)")
            con.execute("create index if not exists attempts_key on attempts(key, attempted)")
            con.execute("create table if not exists waits (key text primary key, until real not null)")

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.execute("pragma journal_mode=wal")
        os.chmod(self.path, 0o600)
        return con

    def reserve(self, key: str, limit: int, window_s: float) -> bool:
        """通信前に計上する。枠を超える場合は False（計上しない）。"""
        now = time.time()
        con = self._con()
        try:
            con.execute("begin immediate")
            w = con.execute("select until from waits where key=?", (key,)).fetchone()
            if w and w[0] > now:
                con.execute("rollback")
                return False
            used = con.execute(
                "select count(*) from attempts where key=? and attempted>?", (key, now - window_s)
            ).fetchone()[0]
            if used >= limit:
                con.execute("rollback")
                return False
            con.execute("insert into attempts(key, attempted) values (?,?)", (key, now))
            con.execute("delete from attempts where attempted<?", (now - 86400 * 2,))
            con.execute("commit")
            return True
        finally:
            con.close()

    def wait_until(self, key: str, until: float) -> None:
        """429等の待機期限を保存（短縮はしない）。"""
        con = self._con()
        try:
            con.execute(
                "insert into waits(key, until) values (?,?) on conflict(key) do update set until=max(until, excluded.until)",
                (key, until),
            )
        finally:
            con.close()

    def waiting_until(self, key: str) -> float:
        con = self._con()
        try:
            r = con.execute("select until from waits where key=?", (key,)).fetchone()
            return r[0] if r and r[0] > time.time() else 0.0
        finally:
            con.close()

    def used(self, key: str, window_s: float) -> int:
        con = self._con()
        try:
            return con.execute(
                "select count(*) from attempts where key=? and attempted>?", (key, time.time() - window_s)
            ).fetchone()[0]
        finally:
            con.close()


class LiveLock:
    """実配信/署名通信を許すインスタンスを同一環境で1つに限定する共通ロック。"""

    def __init__(self) -> None:
        self._fd: int | None = None

    def acquire(self) -> None:
        ensure_private_dir(LOCK_FILE.parent)
        fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise ConfigError("別の実接続インスタンス/試験が共通ロックを保持しています") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None


@dataclass
class ExternalGate:
    """アプリの全外部HTTPはこのゲート経由。既定は拒否。"""

    allow_live: bool = False
    lock: LiveLock | None = None

    def check(self, service: str, url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host_allowed("loopback", host) and service != "loopback":
            raise ExternalDenied("ループバック宛は外部サービス経路として使えません")
        if service == "loopback":
            if not host_allowed("loopback", host):
                raise ExternalDenied(f"ループバック以外は許可されません: {host}")
            return
        if not self.allow_live or self.lock is None or not self.lock.held:
            raise ExternalDenied("外部通信は許可されていません（--allow-live と共通ロックが必要）")
        if not host_allowed(service, host):
            raise ExternalDenied(f"許可されていない宛先です: {host}")

    def client(self, service: str, **kw) -> httpx.AsyncClient:
        async def hook(request: httpx.Request) -> None:
            self.check(service, str(request.url))

        kw.setdefault("timeout", 15.0)
        kw.setdefault("follow_redirects", False)
        kw.setdefault("trust_env", False)
        return httpx.AsyncClient(event_hooks={"request": [hook]}, **kw)
