from alembic import context
from sqlalchemy import create_engine

from live_rec_lab.db.models import metadata


def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
