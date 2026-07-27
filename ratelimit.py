"""
Rate limiting & quota tracking (Section 4).

- Per-key token bucket for burst/sustained rate limiting (in-process, in-memory —
  fine for a single server process; see README for the multi-process caveat).
- Daily/monthly quota counters persisted in SQLite so they survive restarts.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from auth import AuthStore
from config import Config


class QuotaExceeded(Exception):
    pass


@dataclass
class _Bucket:
    capacity: float
    tokens: float
    refill_per_sec: float
    last_refill: float


class TokenBucketLimiter:
    """Pure in-memory token bucket, one per API key. Thread-safe."""

    def __init__(self, default_capacity: int, default_refill_per_sec: float):
        self.default_capacity = default_capacity
        self.default_refill_per_sec = default_refill_per_sec
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, key: str, capacity: int, refill_per_sec: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(
                capacity=capacity,
                tokens=capacity,
                refill_per_sec=refill_per_sec,
                last_refill=time.monotonic(),
            )
            self._buckets[key] = b
        return b

    def try_consume(
        self, key: str, capacity: int | None = None, refill_per_sec: float | None = None, cost: float = 1.0
    ) -> bool:
        # `is not None`, not `or` — see the identical note in RateLimiter.check().
        # A caller explicitly passing refill_per_sec=0.0 means "hard cap, no
        # refill", and must not be silently overridden by the default.
        cap = capacity if capacity is not None else self.default_capacity
        refill = refill_per_sec if refill_per_sec is not None else self.default_refill_per_sec
        with self._lock:
            b = self._get_bucket(key, cap, refill)
            now = time.monotonic()
            elapsed = now - b.last_refill
            b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_sec)
            b.last_refill = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True
            return False


class SqliteQuotaStore:
    """Daily/monthly request counters backed by SQLite.

    Chosen over Redis for this project's scale (single server process,
    modest request volume) — see README "Design Decisions & Tradeoffs" for
    when you'd want to move to Redis instead (multi-process/horizontal
    scaling, needing atomic counters shared across processes).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, timeout=5)
        return self._local.conn

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_counters (
                api_key TEXT NOT NULL,
                period_type TEXT NOT NULL,   -- 'daily' or 'monthly'
                period_key TEXT NOT NULL,    -- e.g. '2026-07-26' or '2026-07'
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (api_key, period_type, period_key)
            )
            """
        )
        conn.commit()
        conn.close()

    def increment_and_check(self, api_key: str, daily_limit: int, monthly_limit: int) -> None:
        now = datetime.now(timezone.utc)
        day_key = now.strftime("%Y-%m-%d")
        month_key = now.strftime("%Y-%m")
        conn = self._conn()
        for period_type, period_key, limit in (
            ("daily", day_key, daily_limit),
            ("monthly", month_key, monthly_limit),
        ):
            cur = conn.execute(
                "SELECT count FROM quota_counters WHERE api_key=? AND period_type=? AND period_key=?",
                (api_key, period_type, period_key),
            )
            row = cur.fetchone()
            current = row[0] if row else 0
            if current >= limit:
                raise QuotaExceeded(
                    f"{period_type} quota exceeded for this API key "
                    f"({current}/{limit} requests used)."
                )
        for period_type, period_key in (("daily", day_key), ("monthly", month_key)):
            conn.execute(
                """
                INSERT INTO quota_counters (api_key, period_type, period_key, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(api_key, period_type, period_key)
                DO UPDATE SET count = count + 1
                """,
                (api_key, period_type, period_key),
            )
        conn.commit()


class RateLimiter:
    """Combines the token bucket (burst control) with the SQLite quota store
    (daily/monthly ceiling), reading per-key overrides from AuthStore."""

    def __init__(self, config: Config, auth_store: AuthStore | None = None):
        self.config = config
        self.auth_store = auth_store
        self.bucket = TokenBucketLimiter(
            config.default_rate_capacity, config.default_rate_refill_per_sec
        )
        self.quota = SqliteQuotaStore(config.db_path)

    def check(self, api_key: str) -> None:
        rec = self.auth_store.record_for(api_key) if self.auth_store else None
        # NOTE: deliberately `is not None`, not `or` — 0.0 is a legitimate
        # "no refill, hard cap" value for rate_refill_per_sec, and `or` would
        # treat that falsy-but-valid 0.0 as "unset" and silently fall back
        # to the default. Same reasoning for the other three fields.
        capacity = rec.rate_capacity if rec and rec.rate_capacity is not None else self.config.default_rate_capacity
        refill = (
            rec.rate_refill_per_sec
            if rec and rec.rate_refill_per_sec is not None
            else self.config.default_rate_refill_per_sec
        )
        daily = rec.daily_quota if rec and rec.daily_quota is not None else self.config.default_daily_quota
        monthly = (
            rec.monthly_quota if rec and rec.monthly_quota is not None else self.config.default_monthly_quota
        )

        if not self.bucket.try_consume(api_key, capacity, refill):
            raise QuotaExceeded(
                f"Rate limit exceeded for this API key (bucket capacity {capacity}, "
                f"refill {refill}/s). Slow down and retry shortly."
            )
        self.quota.increment_and_check(api_key, daily, monthly)
