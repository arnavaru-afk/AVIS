"""Environment-backed configuration for the AVIS Python client."""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClientConfig:
    base_url: str
    jwt_token: str | None
    enabled: bool

    @classmethod
    def from_env(cls) -> "ClientConfig":
        enabled = _parse_bool(os.getenv("AVIS_ENABLED", "true"))
        base_url_env = os.getenv("AVIS_BASE_URL")
        if enabled and (base_url_env is None or not base_url_env.strip()):
            warnings.warn(
                "AVIS_ENABLED is true but AVIS_BASE_URL is not set; defaulting to http://localhost:8000",
                stacklevel=2,
            )
        base_url = (base_url_env or "http://localhost:8000").strip() or "http://localhost:8000"
        jwt_token = os.getenv("AVIS_JWT_TOKEN")
        return cls(
            base_url=base_url.rstrip("/"),
            jwt_token=jwt_token.strip() if jwt_token and jwt_token.strip() else None,
            enabled=enabled,
        )


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() not in {"0", "false", "no", "off"}
