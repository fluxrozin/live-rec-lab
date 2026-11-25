"""SQLite接続とAlembic移行（同期、短いトランザクション）。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, inspect

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class UnknownDatabase(RuntimeError):
    """未知のDB（Alembic版なし・既存テーブルあり）。無断で変更しない。"""


def make_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True, connect_args={"timeout": 10, "check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi: sqlite3.Connection, _rec: object) -> None:  # pragma: no cover - 接続時のみ
        dbapi.isolation_level = None  # SQLAlchemyにトランザクション制御を任せない（BEGINは明示）
        cur = dbapi.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _begin(conn: Connection) -> None:  # pragma: no cover
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def _config(path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(_config(Path("/nonexistent"))).get_current_head() or ""


def current_revision(path: Path) -> str | None:
    """読取専用で現在の版を返す（DBを作成・変更しない）。"""
    if not path.is_file():
        return None
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute("select name from sqlite_master where name='alembic_version'").fetchone()
        if not row:
            return None
        r = con.execute("select version_num from alembic_version").fetchone()
        return r[0] if r else None
    finally:
        con.close()


def migrate(path: Path) -> None:
    """新規DBは初期revisionで作成。既知DBは最新へ。未知のDBは拒否する。"""
    if path.exists() and path.stat().st_size > 0:
        rev = current_revision(path)
        if rev is None:
            raise UnknownDatabase(f"未知のDBです（Alembic版なし）。無断で変更しません: {path}")
    engine = make_engine(path)
    try:
        command.upgrade(_config(path), "head")
    finally:
        engine.dispose()


@contextmanager
def connect(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn


def tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())
