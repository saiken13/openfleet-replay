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
    assert {
        "robots",
        "runs",
        "events",
        "incidents",
        "telemetry_samples",
        "topic_samples",
        "topic_states",
        "node_states",
    } <= _table_names(migrated_database)


def test_concurrent_upgrades_on_a_fresh_database_all_succeed() -> None:
    """Replicas each run `alembic upgrade head` at startup (Dockerfile CMD). On a fresh
    database they used to race to create alembic_version and the loser exited (BUG-006);
    env.py now serializes them with an advisory lock. Real processes, as in production."""
    import os
    import subprocess
    import sys

    import asyncpg
    from sqlalchemy.engine import make_url

    from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL

    url = make_url(TEST_DATABASE_URL).set(database="openfleet_test_migrate_race")

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(
            user=url.username,
            password=url.password,
            host=url.host,
            port=url.port,
            database="postgres",
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    name = url.database
    run_in_fresh_thread(asyncio.run, admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    run_in_fresh_thread(asyncio.run, admin(f'CREATE DATABASE "{name}"'))
    try:
        env = {**os.environ, "OPENFLEET_DATABASE_URL": url.render_as_string(hide_password=False)}
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=BACKEND_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(6)
        ]
        results = [(p.wait(timeout=120), p.stdout.read() if p.stdout else "") for p in procs]
        failed = [out[-1500:] for code, out in results if code != 0]
        assert not failed, failed[0]

        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        versions: list[str] = []

        async def current() -> None:
            conn = await asyncpg.connect(
                user=url.username,
                password=url.password,
                host=url.host,
                port=url.port,
                database=name,
            )
            try:
                versions.extend(
                    r[0] for r in await conn.fetch("SELECT version_num FROM alembic_version")
                )
            finally:
                await conn.close()

        run_in_fresh_thread(asyncio.run, current())
        assert versions == [head]
    finally:
        run_in_fresh_thread(asyncio.run, admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
