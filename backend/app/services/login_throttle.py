"""Failed-login throttling (DECISIONS #027).

Two fixed-window counters per attempt: one per username (normalized, known or not, so a 429
says nothing about whether an account exists) and one per client IP. Each attempt *reserves*
a slot in both, atomically, before the password is checked. That way, concurrent attempts
can't all get past the check while scrypt runs. If either counter is over its limit, the
attempt is refused without checking the password. A successful login clears its username's
counter and gives back its IP slot. The IP counter isn't cleared on success, so an attacker
can't reset it by signing in to their own account between guesses.

Counters live in Redis when `redis_url` is set, so every replica shares them. If Redis is
unreachable, or isn't configured, they fall back to this process's memory: the limits then
apply per replica, but they still apply.
"""

import logging
import math
from datetime import datetime

import redis.asyncio as redis

from app.core.config import Settings

log = logging.getLogger(__name__)

DEFAULT_PREFIX = "openfleet:login"
_REDIS_TIMEOUT_S = 0.5  # a hung Redis must not stall sign-in; we fall back to memory


class MemoryCounters:
    """Window-bucketed counters. Only the current bucket is kept; older ones are dropped."""

    def __init__(self) -> None:
        self._bucket = -1
        self._counts: dict[str, int] = {}

    def _roll(self, bucket: int) -> None:
        if bucket != self._bucket:
            self._bucket, self._counts = bucket, {}

    async def reserve(self, keys: list[str], bucket: int, ttl_s: int) -> list[int]:
        self._roll(bucket)
        for k in keys:
            self._counts[k] = self._counts.get(k, 0) + 1
        return [self._counts[k] for k in keys]

    async def release(self, clear: list[str], decrement: list[str], bucket: int) -> None:
        self._roll(bucket)
        for k in clear:
            self._counts.pop(k, None)
        for k in decrement:
            if self._counts.get(k, 0) > 0:
                self._counts[k] -= 1


class RedisCounters:
    def __init__(self, url: str) -> None:
        self.client = redis.from_url(
            url, socket_timeout=_REDIS_TIMEOUT_S, socket_connect_timeout=_REDIS_TIMEOUT_S
        )

    async def reserve(self, keys: list[str], bucket: int, ttl_s: int) -> list[int]:
        async with self.client.pipeline(transaction=True) as pipe:
            for k in keys:
                pipe.incr(k)
                pipe.expire(k, ttl_s)
            results = await pipe.execute()
        return [int(n) for n in results[::2]]

    async def release(self, clear: list[str], decrement: list[str], bucket: int) -> None:
        async with self.client.pipeline(transaction=True) as pipe:
            for k in clear:
                pipe.delete(k)
            for k in decrement:
                pipe.decr(k)
            await pipe.execute()

    async def close(self) -> None:
        await self.client.aclose()


class LoginThrottle:
    def __init__(
        self, settings: Settings, redis_url: str | None = None, prefix: str = DEFAULT_PREFIX
    ) -> None:
        self.window_s = settings.login_window_s
        self.max_per_user = settings.login_max_failures_per_user
        self.max_per_ip = settings.login_max_failures_per_ip
        self.prefix = prefix
        self.memory = MemoryCounters()
        self.redis = RedisCounters(redis_url) if redis_url else None
        self._redis_ok = True

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.close()

    def _bucket(self, now: datetime) -> int:
        return int(now.timestamp() // self.window_s)

    def _limits(self, username: str, ip: str | None, bucket: int) -> list[tuple[str, str, int]]:
        """(scope, key, limit) for each scope that applies; a limit of 0 disables a scope."""
        out = []
        if self.max_per_user > 0:
            name = username.strip().lower()
            out.append(("user", f"{self.prefix}:user:{bucket}:{name}", self.max_per_user))
        if self.max_per_ip > 0 and ip:
            out.append(("ip", f"{self.prefix}:ip:{bucket}:{ip}", self.max_per_ip))
        return out

    async def _store(self, op: str, *args: object) -> object:
        if self.redis is not None:
            try:
                result = await getattr(self.redis, op)(*args)
            except (redis.RedisError, OSError) as exc:
                if self._redis_ok:
                    log.warning("login throttle: Redis unavailable (%s); counting in memory", exc)
                self._redis_ok = False
            else:
                if not self._redis_ok:
                    log.warning("login throttle: Redis is back")
                self._redis_ok = True
                return result
        return await getattr(self.memory, op)(*args)

    async def attempt(self, username: str, ip: str | None, now: datetime) -> int | None:
        """Reserve this attempt. None to go ahead; otherwise seconds until it may retry."""
        bucket = self._bucket(now)
        limits = self._limits(username, ip, bucket)
        if not limits:
            return None
        ttl_s = math.ceil(self.window_s) + 60  # outlives its bucket even with some clock skew
        counts = await self._store("reserve", [k for _, k, _ in limits], bucket, ttl_s)
        over = [(k, n, lim) for (_, k, lim), n in zip(limits, counts, strict=True) if n > lim]
        if not over:
            return None
        for key, n, limit in over:
            if n == limit + 1:  # once per window and key, not on every refused attempt
                log.warning("login throttled: %s (%d attempts in %ss)", key, limit, self.window_s)
        retry = (bucket + 1) * self.window_s - now.timestamp()
        return max(1, math.ceil(retry))

    async def succeeded(self, username: str, ip: str | None, now: datetime) -> None:
        bucket = self._bucket(now)
        limits = self._limits(username, ip, bucket)
        if limits:
            user_keys = [k for scope, k, _ in limits if scope == "user"]
            ip_keys = [k for scope, k, _ in limits if scope == "ip"]
            await self._store("release", user_keys, ip_keys, bucket)
