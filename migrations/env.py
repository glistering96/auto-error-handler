from alembic import context

from aeh.config import Settings
from aeh.db import Base, make_engine

config = context.config
config.set_main_option("sqlalchemy.url", Settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = make_engine(Settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
