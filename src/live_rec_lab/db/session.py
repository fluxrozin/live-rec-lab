"""SQLite 接続。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from live_rec_lab.config import load_paths

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def _sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA secure_delete=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def reset_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        paths = load_paths()
        paths.ensure()
        _engine = create_engine(
            URL.create("sqlite", database=str(paths.db_path)),
            connect_args={"check_same_thread": False},
        )
        event.listen(_engine, "connect", _sqlite_pragmas)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session_scope(*, write_lock: bool = False) -> Iterator[Session]:
    get_engine()
    assert _factory is not None
    db = _factory()
    try:
        if write_lock:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upgrade_database() -> None:
    from alembic import command
    from alembic.config import Config

    from live_rec_lab.config import load_paths

    paths = load_paths()
    paths.ensure()
    root = None
    for parent in PathParents(__file__):
        if (parent / "alembic.ini").is_file():
            root = parent
            break
    if root is None:
        raise FileNotFoundError("alembic.ini")
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{paths.db_path}".replace("%", "%%"))
    command.upgrade(cfg, "head")


def PathParents(file: str):
    from pathlib import Path

    return Path(file).resolve().parents
