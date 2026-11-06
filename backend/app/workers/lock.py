"""Postgres advisory lock so only one backend replica runs a sweeper tick (DECISIONS #024).

Session-level, not transaction-level: a tick commits once per robot, and a
`pg_advisory_xact_lock` would be released by the first commit. The lock is taken with
`pg_try_advisory_lock` on a dedicated pool connection for the duration of one tick and then
released, so there is no long-lived leader: whichever replica gets there first sweeps, the
others skip that interval, and if a replica dies its lock dies with its connection.

Needs a direct (or session-pooled) connection: a transaction-pooling PgBouncer would hand the
lock's backend to other clients.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

# Arbitrary 64-bit key, unique to the sweeper within this database.
SWEEPER_LOCK_KEY = 0x0F1EE7_5EE9


class AdvisoryLock:
    def __init__(self, engine: AsyncEngine, key: int = SWEEPER_LOCK_KEY) -> None:
        self.engine = engine
        self.key = key

    @asynccontextmanager
    async def try_hold(self) -> AsyncIterator[bool]:
        """Yield True if this process now holds the lock (released on exit), else False."""
        async with self.engine.connect() as conn:
            got = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})
            ).scalar_one()
            await conn.commit()  # the lock is session-level; don't sit idle in a transaction
            try:
                yield bool(got)
            finally:
                if got:
                    try:
                        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
                        await conn.commit()
                    except BaseException:
                        # Can't confirm the unlock (e.g. cancelled mid-call): drop the
                        # connection so Postgres releases the lock with the session, rather
                        # than returning a lock-holding connection to the pool.
                        log.warning(
                            "advisory lock %s: unlock failed; discarding connection", self.key
                        )
                        await conn.invalidate()
                        raise
