from __future__ import annotations

import time

import pytest

from auth import AuthStore, KeyRecord
from ratelimit import QuotaExceeded, RateLimiter, SqliteQuotaStore, TokenBucketLimiter


def test_token_bucket_allows_up_to_capacity_then_blocks():
    bucket = TokenBucketLimiter(default_capacity=3, default_refill_per_sec=0.0)
    assert bucket.try_consume("k") is True
    assert bucket.try_consume("k") is True
    assert bucket.try_consume("k") is True
    assert bucket.try_consume("k") is False  # capacity exhausted, no refill


def test_token_bucket_refills_over_time():
    bucket = TokenBucketLimiter(default_capacity=1, default_refill_per_sec=100.0)
    assert bucket.try_consume("k") is True
    assert bucket.try_consume("k") is False
    time.sleep(0.03)  # ~3 tokens worth at 100/s
    assert bucket.try_consume("k") is True


def test_token_bucket_keys_are_independent():
    bucket = TokenBucketLimiter(default_capacity=1, default_refill_per_sec=0.0)
    assert bucket.try_consume("key-a") is True
    assert bucket.try_consume("key-b") is True  # separate bucket, unaffected by key-a


def test_sqlite_quota_store_enforces_daily_limit(temp_db_path):
    store = SqliteQuotaStore(temp_db_path)
    store.increment_and_check("k", daily_limit=2, monthly_limit=1000)
    store.increment_and_check("k", daily_limit=2, monthly_limit=1000)
    with pytest.raises(QuotaExceeded):
        store.increment_and_check("k", daily_limit=2, monthly_limit=1000)


def test_sqlite_quota_store_persists_across_instances(temp_db_path):
    store1 = SqliteQuotaStore(temp_db_path)
    store1.increment_and_check("k", daily_limit=5, monthly_limit=1000)
    store1.increment_and_check("k", daily_limit=5, monthly_limit=1000)

    # simulate a process restart: brand new store instance, same db file
    store2 = SqliteQuotaStore(temp_db_path)
    with pytest.raises(QuotaExceeded):
        for _ in range(5):
            store2.increment_and_check("k", daily_limit=5, monthly_limit=1000)


def test_ratelimiter_uses_per_key_overrides_from_authstore(test_config):
    auth_store = AuthStore(
        records=[
            KeyRecord(
                api_key="tight",
                scopes=["model:read"],
                rate_capacity=1,
                rate_refill_per_sec=0.0,
                daily_quota=1000,
                monthly_quota=1000,
            )
        ]
    )
    limiter = RateLimiter(test_config, auth_store)
    limiter.check("tight")  # 1st request consumes the only bucket token
    with pytest.raises(QuotaExceeded):
        limiter.check("tight")  # 2nd request: bucket empty, no refill configured


def test_ratelimiter_falls_back_to_config_defaults_for_unlisted_key(test_config):
    limiter = RateLimiter(test_config, AuthStore(records=[]))
    # test_config sets capacity=5, so 5 calls should succeed for an unknown key
    for _ in range(5):
        limiter.check("no-override-key")


def test_ratelimiter_refuses_to_start_with_multiple_workers(test_config):
    # TokenBucketLimiter is pure in-memory, per-process state: with more than
    # one gunicorn worker, each worker gets its own independent bucket, so
    # the real enforced rate silently becomes capacity x worker_count. This
    # must fail loudly at startup rather than quietly under-enforce limits.
    test_config.worker_count = 2
    with pytest.raises(RuntimeError, match="NEXUS_WORKER_COUNT"):
        RateLimiter(test_config)


def test_ratelimiter_starts_fine_with_default_worker_count(test_config):
    # worker_count defaults to 1 (Config()'s default) — the common case must
    # not be affected by the new guard.
    assert test_config.worker_count == 1
    RateLimiter(test_config, AuthStore(records=[]))  # should not raise