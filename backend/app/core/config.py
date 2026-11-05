from functools import lru_cache

from pydantic import Field
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

    # Liveness (server time, DECISIONS #010)
    heartbeat_timeout_s: float = Field(10.0, gt=0)
    sweep_interval_s: float = Field(2.0, gt=0)
    sweeper_enabled: bool = True
    run_abandon_after_s: float = Field(60.0, gt=0)

    # Incidents
    incident_resolve_after_s: float = Field(10.0, ge=0)
    timeline_lookback_s: float = Field(60.0, ge=0)

    # Topic health
    topic_degraded_ratio: float = Field(0.7, gt=0, le=1)
    topic_failed_ratio: float = Field(0.1, ge=0, le=1)
    topic_stale_after_s: float = Field(3.0, gt=0)

    # System metrics
    cpu_high_pct: float = 90.0
    mem_high_pct: float = 90.0
    battery_low_pct: float = 25.0
    battery_critical_pct: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
