"""Alembic 環境。"""

from __future__ import annotations

from sqlalchemy import engine_from_config, pool

from alembic import context
from live_rec_lab.db.models import Base

config = context.config

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # SQLite batch ALTER rebuilds tables. Validate every reference before committing the migration.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA busy_timeout=5000")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        try:
            context.run_migrations()
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise RuntimeError("移行後の外部キーを照合できません")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
