"""Time-driven checks (DECISIONS #006): catches problems that only show up as *missing*
data. Every replica runs a sweeper loop, but each tick first takes a Postgres advisory lock
(DECISIONS #024), so one replica sweeps per interval and the rest skip."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.realtime.hub import Hub
from app.services.liveness_service import LivenessService
from app.workers.lock import AdvisoryLock

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    marked: list[str] = field(default_factory=list)
    aborted_runs: list[str] = field(default_factory=list)
    skipped: bool = False  # another replica held the lock


class Sweeper:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        hub: Hub,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock: AdvisoryLock | None = None,
    ) -> None:
        """`lock=None` sweeps unconditionally (single process / tests); the app passes one."""
        self.sessionmaker = sessionmaker
        self.hub = hub
        self.settings = settings
        self.clock = clock
        self.lock = lock

    async def tick(self, now: datetime | None = None) -> TickResult:
        """One pass. `now` is injectable so tests don't depend on wall-clock time."""
        if self.lock is None:
            return await self._sweep(now or self.clock())
        async with self.lock.try_hold() as held:
            if not held:
                return TickResult(skipped=True)
            # Read the clock after acquiring: waiting on the pool must not make `now` stale.
            return await self._sweep(now or self.clock())

    async def _sweep(self, now: datetime) -> TickResult:
        async with self.sessionmaker() as session:
            svc = LivenessService(session, self.hub, self.settings)
            marked = await svc.mark_stale_offline(now)
            aborted = await svc.abort_abandoned_runs(now)
        return TickResult(marked=marked, aborted_runs=aborted)

    async def run_forever(self) -> None:
        log.info("sweeper started (interval %.1fs)", self.settings.sweep_interval_s)
        while True:
            try:
                result = await self.tick()
                if result.skipped:
                    log.debug("sweeper: another replica holds the lock, skipping")
                elif result.marked or result.aborted_runs:
                    log.info(
                        "sweeper: re-evaluated %s, aborted runs %s",
                        result.marked,
                        result.aborted_runs,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # keep sweeping; one bad tick must not stop liveness checks
                log.exception("sweeper tick failed")
            await asyncio.sleep(self.settings.sweep_interval_s)
