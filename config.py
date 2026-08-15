"""
Central config, loaded from environment variables (see .env.example).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    omniroute_base_url: str = field(
        default_factory=lambda: os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:8787")
    )
    omniroute_api_key: str = field(
        default_factory=lambda: os.environ.get("OMNIROUTE_API_KEY", "")
    )
    default_model: str = field(
        default_factory=lambda: os.environ.get("NEXUS_DEFAULT_MODEL", "gpt-4o-mini")
    )
    fallback_model: str | None = field(
        default_factory=lambda: os.environ.get("NEXUS_FALLBACK_MODEL") or None
    )
    keys_file: str = field(
        default_factory=lambda: os.environ.get("NEXUS_KEYS_FILE", "keys.json")
    )

    # Storage backends
    db_path: str = field(
        default_factory=lambda: os.environ.get("NEXUS_DB_PATH", "nexus_quota.sqlite3")
    )
    cache_backend: str = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_BACKEND", "memory")
    )  # "memory" or "redis"
    redis_url: str = field(
        default_factory=lambda: os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6379/0")
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_TTL_SECONDS", "600"))
    )

    # Rate limiting defaults (per key overrides live in the auth store)
    default_rate_capacity: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_RATE_CAPACITY", "20"))
    )
    default_rate_refill_per_sec: float = field(
        default_factory=lambda: float(os.environ.get("NEXUS_RATE_REFILL_PER_SEC", "0.5"))
    )
    default_daily_quota: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_DAILY_QUOTA", "500"))
    )
    default_monthly_quota: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_MONTHLY_QUOTA", "10000"))
    )

    http_host: str = field(default_factory=lambda: os.environ.get("NEXUS_HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: int(os.environ.get("NEXUS_HTTP_PORT", "8080")))

    # Declares how many gunicorn worker PROCESSES this deployment runs (i.e.
    # whatever number follows `--workers` in your gunicorn command / Dockerfile
    # CMD). Not auto-detected — gunicorn doesn't expose worker count to the
    # app process before forking, so this has to be told explicitly. Used
    # only to fail fast at startup if it's inconsistent with the in-memory
    # token bucket's single-process assumption; see RateLimiter.__init__.
    worker_count: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_WORKER_COUNT", "1"))
    )

    log_level: str = field(default_factory=lambda: os.environ.get("NEXUS_LOG_LEVEL", "INFO"))


config = Config()
