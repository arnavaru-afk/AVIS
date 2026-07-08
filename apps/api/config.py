"""Environment-backed configuration for the AVIS API app."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class SettingsError(RuntimeError):
    """Raised when required API settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    jwt_public_key: str
    avis_env: str = "dev"
    log_level: str = "INFO"

    @property
    def async_database_url(self) -> str:
        if self.database_url.startswith("sqlite+pysqlite://"):
            return self.database_url.replace("sqlite+pysqlite://", "sqlite+aiosqlite://", 1)
        if self.database_url.startswith("sqlite://") and "+" not in self.database_url.split("://", 1)[0]:
            return self.database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.database_url


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise SettingsError(f"Missing required environment variable: {name}")
    return value.strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    avis_env = os.getenv("AVIS_ENV", "dev").strip().lower()
    if avis_env not in {"dev", "staging", "prod"}:
        raise SettingsError("AVIS_ENV must be one of: dev, staging, prod")
    return Settings(
        database_url=_required_env("DATABASE_URL"),
        jwt_public_key=_required_env("AVIS_JWT_PUBLIC_KEY"),
        avis_env=avis_env,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
