"""Thin API client. Every robot-facing endpoint is idempotent, so retrying on network errors
and 5xx is always safe (DECISIONS #013) — this client is the reference for that contract."""

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ApiError(Exception):
    pass


class FleetApi:
    def __init__(
        self,
        base_url: str,
        retries: int = 5,
        backoff_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10, transport=transport)
        self.retries = retries
        self.backoff_s = backoff_s

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, json: Any = None) -> httpx.Response:
        delay = self.backoff_s
        for attempt in range(self.retries + 1):
            try:
                resp = await self.client.request(method, path, json=json)
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
                    return resp
                reason = f"HTTP {resp.status_code}"
            except httpx.TransportError as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt == self.retries:
                raise ApiError(f"{method} {path} failed after {self.retries} retries: {reason}")
            log.warning("%s %s: %s; retrying in %.1fs", method, path, reason, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)
        raise AssertionError("unreachable")

    async def wait_until_healthy(self, timeout_s: float = 60) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            try:
                resp = await self.client.get("/api/v1/health")
                if resp.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise ApiError("API did not become healthy in time")
            await asyncio.sleep(1)

    async def register(self, robot_id: str, name: str, model: str) -> None:
        await self._request("PUT", f"/api/v1/robots/{robot_id}", {"name": name, "model": model})

    async def heartbeat(self, robot_id: str, software_version: str | None) -> None:
        body = {"software_version": software_version} if software_version else {}
        await self._request("POST", f"/api/v1/robots/{robot_id}/heartbeat", body)

    async def start_run(
        self, run_id: str, robot_id: str, software_version: str, started_at: str
    ) -> None:
        await self._request(
            "PUT",
            f"/api/v1/runs/{run_id}",
            {"robot_id": robot_id, "software_version": software_version, "started_at": started_at},
        )

    async def telemetry(self, run_id: str, batch: dict[str, Any]) -> None:
        await self._request("POST", f"/api/v1/runs/{run_id}/telemetry", batch)

    async def end_run(self, run_id: str, status: str, ended_at: str) -> None:
        await self._request(
            "POST", f"/api/v1/runs/{run_id}/end", {"status": status, "ended_at": ended_at}
        )
