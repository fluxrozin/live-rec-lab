"""Cross-process ledger for explicitly scoped external service operations."""

import fcntl
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_requests (scope TEXT NOT NULL, charged_at_us INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS external_requests_window ON external_requests (scope, charged_at_us);
CREATE TABLE IF NOT EXISTS external_cooldowns (scope TEXT PRIMARY KEY, available_at_us INTEGER NOT NULL);
"""


class BudgetDenied(ValueError):
    """An external call is outside its authorized budget."""


@dataclass(frozen=True)
class LivePermission:
    service: str
    target: str
    operations: frozenset[str]
    expires: datetime

    def allows(self, service: str, target: str, operation: str, now: datetime) -> bool:
        return self.service == service and self.target == target and operation in self.operations and self.expires > now


@dataclass(frozen=True)
class Limit:
    scope: str
    requests: int
    window: timedelta


def _instant(value: datetime) -> int:
    if value.utcoffset() is None:
        raise ValueError("external budget requires a timezone-aware instant")
    return (value - _EPOCH) // timedelta(microseconds=1)


def _private_file(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and details.st_nlink == 1
        and not details.st_mode & 0o077
    )


class ActiveBudget:
    def __init__(self, database: sqlite3.Connection, scopes: set[str]) -> None:
        self._database = database
        self._scopes = scopes

    def defer(self, scope: str, until: datetime) -> None:
        if scope not in self._scopes:
            raise BudgetDenied("unknown cooldown scope")
        with self._database:
            self._database.execute(
                """
                INSERT INTO external_cooldowns VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE
                SET available_at_us = MAX(external_cooldowns.available_at_us, excluded.available_at_us)
                """,
                (scope, _instant(until)),
            )


class ExternalBudget:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _check_location(self) -> None:
        directory = self.path.parent
        owner = directory.lstat()
        if (
            directory.resolve() != directory
            or not stat.S_ISDIR(owner.st_mode)
            or owner.st_uid != os.geteuid()
            or owner.st_mode & 0o077
        ):
            raise BudgetDenied("external budget requires an owned private shared directory")
        for candidate in (self.path, *directory.glob(self.path.name + "-*")):
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                continue
            if not _private_file(details):
                raise BudgetDenied("external budget file is not owned and private")

    @contextmanager
    def connection(
        self,
        permission: LivePermission | None,
        service: str,
        target: str,
        operation: str,
        limits: tuple[Limit, ...],
        *,
        now: datetime | None = None,
    ) -> Iterator[ActiveBudget]:
        started = now if now is not None else datetime.now(UTC)
        if permission is None or not permission.allows(service, target, operation, started):
            raise BudgetDenied("permission is absent, expired, or does not match the request")
        if not limits or any(limit.requests < 1 or limit.window <= timedelta(0) for limit in limits):
            raise BudgetDenied("a valid external quota is required")
        lock_path = self.path.with_suffix(".lock")
        self._check_location()
        lock = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not _private_file(os.fstat(lock)):
                raise BudgetDenied("external budget lock is not owned and private")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BudgetDenied("external connection is busy") from error
            self._check_location()
            database_fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            os.close(database_fd)
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as database:
                database.executescript(_SCHEMA)
                scopes = {limit.scope for limit in limits}
                self._charge(database, limits, scopes, _instant(started))
                yield ActiveBudget(database, scopes)
        finally:
            os.close(lock)

    @staticmethod
    def _charge(database: sqlite3.Connection, limits: tuple[Limit, ...], scopes: set[str], started: int) -> None:
        with database:
            database.execute("BEGIN IMMEDIATE")
            for limit in limits:
                available = database.execute(
                    "SELECT available_at_us FROM external_cooldowns WHERE scope = ?", (limit.scope,)
                ).fetchone()
                if available and started < available[0]:
                    raise BudgetDenied("external cooldown is still active")
                cutoff = started - limit.window // timedelta(microseconds=1)
                count = database.execute(
                    "SELECT COUNT(*) FROM external_requests WHERE scope = ? AND charged_at_us > ?",
                    (limit.scope, cutoff),
                ).fetchone()[0]
                if count >= limit.requests:
                    raise BudgetDenied("external quota is exhausted")
            database.executemany(
                "INSERT INTO external_requests VALUES (?, ?)", ((scope, started) for scope in sorted(scopes))
            )
