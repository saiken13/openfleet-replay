"""Thin API client. Every robot-facing endpoint is idempotent, so retrying on network errors
and 5xx is always safe (DECISIONS #013) — this client is the reference for that contract.

Each robot signs its requests with its own API key (DECISIONS #021); one simulator process
speaks for many robots, so it holds a robot_id -> key map."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ApiError(Exception):
    pass


class FleetApi:
    def __init__(
        self,
        base_url: str,
        keys: dict[str, str] | None = None,
        retries: int = 5,
        backoff_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10, transport=transport)
        self.keys = keys or {}
        self.retries = retries
        self.backoff_s = backoff_s
        self._run_robot: dict[str, str] = {}  # run_id -> robot_id, to sign run requests

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self, robot_id: str) -> dict[str, str]:
        key = self.keys.get(robot_id)
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _request(
        self, method: str, path: str, robot_id: str, json: Any = None
    ) -> httpx.Response:
        delay = self.backoff_s
        headers = self._headers(robot_id)
        for attempt in range(self.retries + 1):
            try:
                resp = await self.client.request(method, path, json=json, headers=headers)
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
        await self._request(
            "PUT", f"/api/v1/robots/{robot_id}", robot_id, {"name": name, "model": model}
        )

    async def heartbeat(self, robot_id: str, software_version: str | None) -> None:
        body = {"software_version": software_version} if software_version else {}
        await self._request("POST", f"/api/v1/robots/{robot_id}/heartbeat", robot_id, body)

    async def start_run(
        self, run_id: str, robot_id: str, software_version: str, started_at: str
    ) -> None:
        self._run_robot[run_id] = robot_id
        await self._request(
            "PUT",
            f"/api/v1/runs/{run_id}",
            robot_id,
            {"robot_id": robot_id, "software_version": software_version, "started_at": started_at},
        )

    async def telemetry(self, run_id: str, batch: dict[str, Any]) -> None:
        await self._request(
            "POST", f"/api/v1/runs/{run_id}/telemetry", self._run_robot[run_id], batch
        )

    async def end_run(self, run_id: str, status: str, ended_at: str) -> None:
        await self._request(
            "POST",
            f"/api/v1/runs/{run_id}/end",
            self._run_robot[run_id],
            {"status": status, "ended_at": ended_at},
        )


def load_keys(path: str | None) -> dict[str, str]:
    """A JSON object of robot_id -> API key, e.g. examples/demo-robot-keys.json."""
    if not path:
        raise ApiError(
            "no robot API keys: pass --keys FILE (e.g. examples/demo-robot-keys.json) "
            "or set OPENFLEET_ROBOT_KEYS_FILE"
        )
    try:
        keys = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ApiError(f"cannot read keys file {path}: {exc}") from exc
    if not isinstance(keys, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
    ):
        raise ApiError(f"{path}: expected a JSON object of robot_id -> key")
    return keys
