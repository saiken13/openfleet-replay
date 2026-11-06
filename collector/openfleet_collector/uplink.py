"""Reliable delivery of batches to the OpenFleet API.

- Batches are sent strictly in order from a bounded buffer; while the API is unreachable they
  accumulate (oldest dropped beyond `buffer_max`) and are re-sent with the SAME seq, which the
  server deduplicates (`UNIQUE(run_id, seq)`), so retries are always safe.
- Network errors / 5xx: back off and retry the same batch.
- 409 on telemetry (the run is no longer RUNNING, e.g. the sweeper aborted it during a long
  outage) or 404 (robot/run unknown, e.g. the server's database was reset): start a fresh run
  and drop batches that belonged to the dead run.
- Other 4xx: the batch itself is invalid; log and drop it (retrying would never succeed).

`pump()` does one unit of work and is what tests drive; `run_forever()` loops it in a thread.
"""

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


class Uplink:
    def __init__(
        self,
        api_url: str,
        robot_id: str,
        *,
        robot_name: str | None = None,
        robot_model: str | None = None,
        software_version: str | None = None,
        buffer_max: int = 600,
        timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self.robot_id = robot_id
        self.robot_name = robot_name or robot_id
        self.robot_model = robot_model
        self.software_version = software_version
        self.client = httpx.Client(base_url=api_url, timeout=timeout_s, transport=transport)
        self.clock = clock
        # Default sleep wakes up early on stop(); tests inject a no-op.
        self.sleep = sleep or self._interruptible_sleep
        self.buffer_max = buffer_max

        self._lock = threading.Lock()
        self._queue: deque[tuple[str, dict[str, Any]]] = deque()  # (run_id, batch)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.run_id: str | None = None
        self._seq = 0
        self._registered = False
        self._backoff = 0.5
        self.dropped = 0
        self.sent = 0

    # --- producer side (batch timer) -----------------------------------------------------

    def next_seq(self) -> int:
        with self._lock:
            seq = self._seq
            self._seq += 1
            return seq

    def enqueue(self, batch: dict[str, Any]) -> None:
        with self._lock:
            if self.run_id is None:
                return  # not started yet; nothing to attach the batch to
            self._queue.append((self.run_id, batch))
            while len(self._queue) > self.buffer_max:
                self._queue.popleft()
                self.dropped += 1
        self._wake.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    # --- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        """Register the robot and open a run. Retries until it succeeds or stop() is called."""
        while not self._stop.is_set():
            try:
                self._start_run()
                return
            except (httpx.TransportError, _Retryable) as exc:
                log.warning("cannot reach OpenFleet (%s); retrying in %.1fs", exc, self._backoff)
                self._wait_backoff()

    def _start_run(self) -> None:
        if not self._registered:
            body: dict[str, Any] = {"name": self.robot_name}
            if self.robot_model:
                body["model"] = self.robot_model
            self._check(self.client.put(f"/api/v1/robots/{self.robot_id}", json=body))
            self._registered = True
        run_id = str(uuid.uuid4())
        body = {"robot_id": self.robot_id, "started_at": self.clock().isoformat()}
        if self.software_version:
            body["software_version"] = self.software_version
        self._check(self.client.put(f"/api/v1/runs/{run_id}", json=body))
        with self._lock:
            self.run_id = run_id
            self._seq = 0
        self._backoff = 0.5
        log.info("run %s started for %s", run_id, self.robot_id)

    def end(self, status: str = "SUCCESS", flush_timeout_s: float = 5.0) -> None:
        """Flush what we can, then end the run (best effort, bounded time)."""
        deadline = time.monotonic() + flush_timeout_s
        while self.pending() and time.monotonic() < deadline:
            if not self.pump():
                break
        run_id = self.run_id
        if run_id is None:
            return
        try:
            self.client.post(
                f"/api/v1/runs/{run_id}/end",
                json={"status": status, "ended_at": self.clock().isoformat()},
            )
        except httpx.HTTPError as exc:
            log.warning("could not end run %s: %s", run_id, exc)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def close(self) -> None:
        self.client.close()

    # --- consumer side ---------------------------------------------------------------------

    def pump(self) -> bool:
        """Try to deliver the oldest batch. Returns True if progress was made."""
        with self._lock:
            if not self._queue:
                return False
            run_id, batch = self._queue[0]
        try:
            resp = self.client.post(f"/api/v1/runs/{run_id}/telemetry", json=batch)
        except httpx.TransportError as exc:
            log.warning("telemetry send failed (%s); %d buffered", exc, self.pending())
            return False

        if resp.status_code < 300:
            self._pop_if_head(run_id, batch)
            self.sent += 1
            self._backoff = 0.5
            return True
        if resp.status_code >= 500:
            log.warning("server error %d; %d buffered", resp.status_code, self.pending())
            return False
        if resp.status_code in (404, 409):
            log.warning(
                "run %s rejected (%d: %s); starting a new run", run_id, resp.status_code, resp.text
            )
            if resp.status_code == 404:
                self._registered = False
            self._drop_run(run_id)
            try:
                self._start_run()
            except (httpx.TransportError, _Retryable) as exc:
                log.warning("could not start a new run yet: %s", exc)
                return False
            return True
        log.error("batch rejected (%d): %s; dropping it", resp.status_code, resp.text[:300])
        self._pop_if_head(run_id, batch)
        self.dropped += 1
        return True

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if self.pump():
                continue
            if self.pending():
                self._wait_backoff()
            else:
                self._wake.wait(timeout=1.0)
                self._wake.clear()

    # --- helpers ---------------------------------------------------------------------------

    def _pop_if_head(self, run_id: str, batch: dict[str, Any]) -> None:
        with self._lock:
            if self._queue and self._queue[0][0] == run_id and self._queue[0][1] is batch:
                self._queue.popleft()

    def _drop_run(self, run_id: str) -> None:
        with self._lock:
            before = len(self._queue)
            self._queue = deque(item for item in self._queue if item[0] != run_id)
            self.dropped += before - len(self._queue)
            if self.run_id == run_id:
                self.run_id = None

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _wait_backoff(self) -> None:
        self.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, 30.0)

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}")
        resp.raise_for_status()


class _Retryable(Exception):
    pass
