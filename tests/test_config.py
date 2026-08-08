from __future__ import annotations

import importlib

import config as config_module
from config import _bool


def _reload_config():
    """Config's dataclass fields are built from os.environ at import/instantiation
    time, so tests need a fresh Config() (not the module-level singleton) after
    changing environment variables."""
    importlib.reload(config_module)
    return config_module.Config()


def test_defaults_when_no_env_vars_set(monkeypatch):
    for var in [
        "OMNIROUTE_BASE_URL",
        "OMNIROUTE_API_KEY",
        "NEXUS_DEFAULT_MODEL",
        "NEXUS_FALLBACK_MODEL",
        "NEXUS_KEYS_FILE",
        "NEXUS_DB_PATH",
        "NEXUS_CACHE_BACKEND",
        "NEXUS_RATE_CAPACITY",
        "NEXUS_DAILY_QUOTA",
        "NEXUS_HTTP_PORT",
        "NEXUS_LOG_LEVEL",
    ]:
        monkeypatch.delenv(var, raising=False)

    cfg = _reload_config()
    assert cfg.omniroute_base_url == "http://localhost:8787"
    assert cfg.omniroute_api_key == ""
    assert cfg.default_model == "gpt-4o-mini"
    assert cfg.fallback_model is None
    assert cfg.keys_file == "keys.json"
    assert cfg.db_path == "nexus_quota.sqlite3"
    assert cfg.cache_backend == "memory"
    assert cfg.default_rate_capacity == 20
    assert cfg.default_daily_quota == 500
    assert cfg.http_port == 8080
    assert cfg.log_level == "INFO"


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "https://omniroute.internal")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-123")
    monkeypatch.setenv("NEXUS_DEFAULT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("NEXUS_CACHE_BACKEND", "redis")
    monkeypatch.setenv("NEXUS_RATE_CAPACITY", "50")
    monkeypatch.setenv("NEXUS_HTTP_PORT", "9090")

    cfg = _reload_config()
    assert cfg.omniroute_base_url == "https://omniroute.internal"
    assert cfg.omniroute_api_key == "sk-test-123"
    assert cfg.default_model == "claude-sonnet-5"
    assert cfg.cache_backend == "redis"
    assert cfg.default_rate_capacity == 50
    assert cfg.http_port == 9090


def test_fallback_model_blank_env_var_is_none(monkeypatch):
    # NEXUS_FALLBACK_MODEL="" should behave the same as unset, per the
    # "or None" in config.py's default_factory.
    monkeypatch.setenv("NEXUS_FALLBACK_MODEL", "")
    cfg = _reload_config()
    assert cfg.fallback_model is None


def test_fallback_model_set_is_preserved(monkeypatch):
    monkeypatch.setenv("NEXUS_FALLBACK_MODEL", "gpt-4o")
    cfg = _reload_config()
    assert cfg.fallback_model == "gpt-4o"


def test_numeric_env_vars_are_cast_to_correct_types(monkeypatch):
    monkeypatch.setenv("NEXUS_CACHE_TTL_SECONDS", "1200")
    monkeypatch.setenv("NEXUS_RATE_REFILL_PER_SEC", "2.5")
    monkeypatch.setenv("NEXUS_MONTHLY_QUOTA", "99999")

    cfg = _reload_config()
    assert cfg.cache_ttl_seconds == 1200
    assert isinstance(cfg.cache_ttl_seconds, int)
    assert cfg.default_rate_refill_per_sec == 2.5
    assert isinstance(cfg.default_rate_refill_per_sec, float)
    assert cfg.default_monthly_quota == 99999
    assert isinstance(cfg.default_monthly_quota, int)


class TestBoolHelper:
    """Direct tests for _bool(), since it's the one piece of parsing logic
    in config.py that isn't a straight os.environ.get()."""

    def test_none_returns_default(self):
        assert _bool(None, True) is True
        assert _bool(None, False) is False

    def test_truthy_strings(self):
        for val in ["1", "true", "True", "TRUE", "yes", "YES", "on", "On"]:
            assert _bool(val, False) is True

    def test_falsy_strings_return_false_regardless_of_default(self):
        for val in ["0", "false", "no", "off", "garbage"]:
            assert _bool(val, True) is False

    def test_whitespace_is_stripped(self):
        assert _bool("  true  ", False) is True
        assert _bool("  ", True) is False
