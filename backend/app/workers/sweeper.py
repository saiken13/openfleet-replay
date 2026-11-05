"""Time-driven checks (DECISIONS #006): catches problems that only show up as *missing*
data. Runs once per process (ARCHITECTURE §10: needs an advisory lock before multi-replica)."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.realtime.hub import Hub
from app.services.liveness_service import LivenessService

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    marked: list[str] = field(default_factory=list)
    aborted_runs: list[str] = field(default_factory=list)


class Sweeper:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        hub: Hub,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessionmaker = sessionmaker
        self.hub = hub
        self.settings = settings
        self.clock = clock

    async def tick(self, now: datetime | None = None) -> TickResult:
        """One pass. `now` is injectable so tests don't depend on wall-clock time."""
        now = now or self.clock()
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
                if result.marked or result.aborted_runs:
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
