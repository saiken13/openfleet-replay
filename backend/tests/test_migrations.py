"""Every migration must upgrade and downgrade cleanly (CONSTRAINTS: schema changes)."""

import asyncio

from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.conftest import alembic_config, run_in_fresh_thread


def _table_names(url: str) -> set[str]:
    async def go() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        finally:
            await engine.dispose()

    names: set[str] = set()
    run_in_fresh_thread(lambda: names.update(asyncio.run(go())))
    return names


def test_migrations_round_trip(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    revisions = list(ScriptDirectory.from_config(cfg).walk_revisions())

    # Walk down one revision at a time (each downgrade() runs), then all the way back up.
    for _ in revisions:
        run_in_fresh_thread(command.downgrade, cfg, "-1")
    assert _table_names(migrated_database) == {"alembic_version"}

    run_in_fresh_thread(command.upgrade, cfg, "head")
    assert {"robots", "runs"} <= _table_names(migrated_database)
