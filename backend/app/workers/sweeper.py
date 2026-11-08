"""Time-driven checks (DECISIONS #006): catches problems that only show up as *missing*
data. Every replica runs a sweeper loop, but each tick first takes a Postgres advisory lock
(DECISIONS #024), so one replica sweeps per interval and the rest skip."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.realtime.hub import Hub
from app.services.liveness_service import LivenessService
from app.services.retention_service import RetentionService
from app.workers.lock import AdvisoryLock

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    marked: list[str] = field(default_factory=list)
    aborted_runs: list[str] = field(default_factory=list)
    compacted_runs: list[str] = field(default_factory=list)
    skipped: bool = False  # another replica held the lock


class Sweeper:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        hub: Hub,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock: AdvisoryLock | None = None,
        listening_since: datetime | None = None,
    ) -> None:
        """`lock=None` sweeps unconditionally (single process / tests); the app passes one.

        `listening_since` (BUG-007): when this process started receiving. Silence is judged
        only once the process has listened for the whole timeout, so a backend that was
        down doesn't blame robots for the outage: they were buffering, not silent. A robot
        that really died is still caught, at most one timeout later than otherwise. None
        disables the grace (tests that drive `now` themselves)."""
        self.sessionmaker = sessionmaker
        self.hub = hub
        self.settings = settings
        self.clock = clock
        self.lock = lock
        self.listening_since = listening_since
        self._next_retention: datetime | None = None  # per process; at most once per interval

    async def tick(self, now: datetime | None = None) -> TickResult:
        """One pass. `now` is injectable so tests don't depend on wall-clock time."""
        if self.lock is None:
            return await self._sweep(now or self.clock())
        async with self.lock.try_hold() as held:
            if not held:
                return TickResult(skipped=True)
            # Read the clock after acquiring: waiting on the pool must not make `now` stale.
            return await self._sweep(now or self.clock())

    def _heard_for(self, now: datetime, seconds: float) -> bool:
        """Has this process been listening for at least `seconds`?"""
        return self.listening_since is None or now - self.listening_since >= timedelta(
            seconds=seconds
        )

    async def _sweep(self, now: datetime) -> TickResult:
        s = self.settings
        marked: list[str] = []
        aborted: list[str] = []
        async with self.sessionmaker() as session:
            svc = LivenessService(session, self.hub, s)
            if self._heard_for(now, s.heartbeat_timeout_s):
                marked = await svc.mark_stale_offline(now)
            if self._heard_for(now, s.run_abandon_after_s):
                aborted = await svc.abort_abandoned_runs(now)
        return TickResult(
            marked=marked, aborted_runs=aborted, compacted_runs=await self._retention(now)
        )

    async def _retention(self, now: datetime) -> list[str]:
        """DECISIONS #030. Runs inside the tick, so under the same advisory lock."""
        s = self.settings
        if s.retention_days is None:
            return []
        if self._next_retention is not None and now < self._next_retention:
            return []
        self._next_retention = now + timedelta(seconds=s.retention_interval_s)
        async with self.sessionmaker() as session:
            done = await RetentionService(session).run(
                now, timedelta(days=s.retention_days), s.retention_batch_runs
            )
        return [str(c.run_id) for c in done]

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
                if result.compacted_runs:
                    log.info("retention: compacted %d runs", len(result.compacted_runs))
            except asyncio.CancelledError:
                raise
            except Exception:  # keep sweeping; one bad tick must not stop liveness checks
                log.exception("sweeper tick failed")
            await asyncio.sleep(self.settings.sweep_interval_s)
