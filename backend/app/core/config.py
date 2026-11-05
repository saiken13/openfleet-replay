from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Read from OPENFLEET_* environment variables (or .env)."""

    model_config = SettingsConfigDict(env_prefix="OPENFLEET_", env_file=".env", extra="ignore")

    # Local-development default. Real deployments set OPENFLEET_DATABASE_URL.
    database_url: str = "postgresql+asyncpg://openfleet:openfleet@localhost:5433/openfleet"
    db_connect_timeout_s: float = 3.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
