import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import get_settings
from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    # Keep the app's loggers when migrations run in-process (tests, CLI).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Tests (and tools) can point Alembic at another database via the config's sqlalchemy.url.
DATABASE_URL = config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# Every backend replica runs `alembic upgrade head` at startup (Dockerfile CMD). Without a
# lock, replicas starting together on a fresh database race to create alembic_version and the
# loser exits (BUG-006). A transaction-level advisory lock serializes them: the next one waits,
# then reads the version the first one committed and has nothing left to do. Its own key,
# distinct from the sweeper's (app/workers/lock.py).
MIGRATION_LOCK_KEY = 0x0F1EE7_A1E8


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Inside alembic's transaction, before it reads the current version: released at
        # commit or rollback, so a crashed migration can't leave it held.
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
