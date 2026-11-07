from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Read from OPENFLEET_* environment variables (or .env).
    Every timing and threshold lives here, never inline (CONSTRAINTS)."""

    model_config = SettingsConfigDict(env_prefix="OPENFLEET_", env_file=".env", extra="ignore")

    # Local-development default. Real deployments set OPENFLEET_DATABASE_URL.
    database_url: str = "postgresql+asyncpg://openfleet:openfleet@localhost:5433/openfleet"
    db_connect_timeout_s: float = 3.0
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:5173"]

    # Auth (DECISIONS #021). Optional JSON file {robot_id: api_key} seeded at startup, for
    # demo/dev setups only; real deployments issue keys with `openfleet keys create`.
    robot_keys_file: Path | None = None
    # Users (DECISIONS #022). Optional JSON {username: {password, role, token?}} seeded at
    # startup, demo/dev only; real deployments use `openfleet users create`.
    users_file: Path | None = None
    # Sessions (DECISIONS #029): a session expires after SESSION_TTL_S without use, and in
    # any case SESSION_MAX_S after sign-in. Use extends it at most once per SESSION_REFRESH_S
    # (so a busy dashboard doesn't write on every request).
    session_ttl_s: float = Field(12 * 3600, gt=0)
    session_max_s: float = Field(7 * 24 * 3600, gt=0)
    session_refresh_s: float = Field(300.0, ge=0)
    cookie_secure: bool = False  # set true behind HTTPS
    login_hint: str | None = None  # shown on the login page (e.g. demo credentials)
    ws_reauth_interval_s: float = Field(30.0, gt=0)  # live sockets re-check their session
    # Login throttling (DECISIONS #027): attempts per fixed window, per username and per
    # client IP. A successful login doesn't count. 0 disables that scope.
    login_window_s: float = Field(300.0, gt=0)
    login_max_failures_per_user: int = Field(5, ge=0)
    login_max_failures_per_ip: int = Field(20, ge=0)

    # Liveness (server time, DECISIONS #010)
    heartbeat_timeout_s: float = Field(10.0, gt=0)
    # Heartbeats during a run are stored as spans (DECISIONS #028); ones at most this far
    # apart merge into one row. Replays with a heartbeat timeout below this can't see gaps
    # shorter than it.
    heartbeat_span_gap_s: float = Field(5.0, gt=0)
    sweep_interval_s: float = Field(2.0, gt=0)
    sweeper_enabled: bool = True
    # Realtime fan-out between replicas (DECISIONS #025). Unset: in-process only, which is
    # correct for a single backend process.
    redis_url: str | None = None
    run_abandon_after_s: float = Field(60.0, gt=0)

    # Retention (DECISIONS #030). Unset: keep everything. Otherwise finished runs that ended
    # more than this many days ago are compacted: summary frozen, raw telemetry deleted.
    retention_days: float | None = Field(None, gt=0)
    retention_interval_s: float = Field(3600.0, gt=0)  # how often the sweeper looks
    retention_batch_runs: int = Field(20, gt=0)  # runs compacted per pass

    # Incidents
    incident_resolve_after_s: float = Field(10.0, ge=0)
    timeline_lookback_s: float = Field(60.0, ge=0)
    # Re-detection replay (DECISIONS #023) loads a whole run into memory; ~14 h at 1 Hz.
    redetect_max_samples: int = Field(50_000, gt=0)

    # Topic health
    topic_degraded_ratio: float = Field(0.7, gt=0, le=1)
    topic_failed_ratio: float = Field(0.1, ge=0, le=1)
    topic_stale_after_s: float = Field(3.0, gt=0)

    # System metrics
    cpu_high_pct: float = 90.0
    mem_high_pct: float = 90.0
    battery_low_pct: float = 25.0
    battery_critical_pct: float = 10.0

    @model_validator(mode="after")
    def _session_bounds(self) -> "Settings":
        if self.session_max_s < self.session_ttl_s:
            raise ValueError("session_max_s must be at least session_ttl_s")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
