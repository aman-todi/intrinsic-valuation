"""Alembic environment (spec §9.2).

Migrations run synchronously over psycopg3 (``postgresql+psycopg://``) against ``DATABASE_URL`` —
the same URL the app uses (``-x dburl=...`` or ``sqlalchemy.url`` override it, e.g. in tests).
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.db.base import to_psycopg_url as normalize_url
from app.db.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    x_url = context.get_x_argument(as_dictionary=True).get("dburl")
    if x_url:
        return normalize_url(x_url)
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return normalize_url(ini_url)
    from app.config import settings

    return normalize_url(settings.DATABASE_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
