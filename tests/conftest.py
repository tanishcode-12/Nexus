from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import AuthStore, KeyRecord
from cache import InMemoryCache
from config import Config
from metrics import MetricsRecorder
from omniroute_client import OmniRouteClient
from ratelimit import RateLimiter
from registry import discover_tools
from tests.fake_omniroute_server import FakeOmniRouteServer


@pytest.fixture(scope="session", autouse=True)
def _ensure_tools_discovered():
    """Populate the global tool registry exactly once for the whole test
    session (idempotent import, so safe even if called again)."""
    discover_tools("tools")


@pytest.fixture(scope="session")
def fake_omniroute():
    server = FakeOmniRouteServer().start()
    yield server
    server.stop()


@pytest.fixture
def omniroute_client(fake_omniroute):
    client = OmniRouteClient(
        base_url=fake_omniroute.base_url,
        api_key="test-omniroute-key",
        default_model="gpt-4o-mini",
        fallback_model=None,
        max_retries=2,
        backoff_base_seconds=0.01,  # fast retries in tests
    )
    yield client


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "quota_test.sqlite3")


@pytest.fixture
def auth_store():
    return AuthStore(
        records=[
            KeyRecord(api_key="key-admin", scopes=["admin"]),
            KeyRecord(api_key="key-readonly", scopes=["model:read"]),
            KeyRecord(
                api_key="key-tight-quota",
                scopes=["model:read"],
                rate_capacity=2,
                rate_refill_per_sec=0.001,
                daily_quota=2,
                monthly_quota=100,
            ),
        ]
    )


@pytest.fixture
def test_config(temp_db_path):
    cfg = Config()
    cfg.db_path = temp_db_path
    cfg.default_rate_capacity = 5
    cfg.default_rate_refill_per_sec = 100.0  # effectively unlimited unless testing burst
    cfg.default_daily_quota = 500
    cfg.default_monthly_quota = 10000
    return cfg


@pytest.fixture
def rate_limiter(test_config, auth_store):
    return RateLimiter(test_config, auth_store)


@pytest.fixture
def memory_cache():
    return InMemoryCache(default_ttl_seconds=600)


@pytest.fixture
def metrics():
    return MetricsRecorder()
