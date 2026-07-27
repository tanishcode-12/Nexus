"""
Regression test for a real bug: http_main.py's _build_default_server() used
to hardcode AuthStore(keys_file="keys.json") instead of reading
config.keys_file, so NEXUS_KEYS_FILE was silently ignored for the HTTP
transport (stdio_main.py was fine — it read the env var directly, before
being refactored to also go through config.keys_file). Every request came
back 401 regardless of a correctly-configured keys file. Caught by actually
running the load test against a live instance, not by any injected-fake
test, because every Flask integration test builds its app via build_app()
directly with a hand-constructed AuthStore, which bypasses this wiring
entirely. This test exercises the real wiring path instead.

Note on approach: config.py's `config` is a module-level singleton built
once at first import (env vars read into it via dataclass default_factory
at construction time), so monkeypatching os.environ mid-process wouldn't
affect it — same reason NEXUS_KEYS_FILE was silently ignored in the first
place. Mutating the singleton's fields directly (with save/restore) is the
correct way to exercise this without a subprocess.
"""
from __future__ import annotations

import json

import pytest

import config as config_module
import http_main


@pytest.fixture
def _saved_config():
    """Snapshot + restore every field http_main._build_default_server()
    reads, so this test can't leak state into any other test."""
    cfg = config_module.config
    saved = {
        "keys_file": cfg.keys_file,
        "omniroute_base_url": cfg.omniroute_base_url,
        "omniroute_api_key": cfg.omniroute_api_key,
        "db_path": cfg.db_path,
        "cache_backend": cfg.cache_backend,
    }
    yield cfg
    for k, v in saved.items():
        setattr(cfg, k, v)


def test_default_server_construction_respects_config_keys_file(
    _saved_config, fake_omniroute, tmp_path
):
    keys_file = tmp_path / "wiring_test_keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"api_key": "sk-wired-correctly", "scopes": ["admin"]}]})
    )

    cfg = _saved_config
    cfg.keys_file = str(keys_file)
    cfg.omniroute_base_url = fake_omniroute.base_url
    cfg.omniroute_api_key = "irrelevant-for-fake-server"
    cfg.db_path = str(tmp_path / "wiring_test_quota.sqlite3")
    cfg.cache_backend = "memory"

    flask_app, _server = http_main._build_default_server()
    client = flask_app.test_client()

    # the whole point: a key from the CONFIGURED keys file must authenticate
    resp = client.get("/v1/tools", headers={"X-API-Key": "sk-wired-correctly"})
    assert resp.status_code == 200

    # and a key NOT in that file must still be rejected (proves it's really
    # reading the file, not just accepting everything)
    resp2 = client.get("/v1/tools", headers={"X-API-Key": "sk-not-in-this-file"})
    assert resp2.status_code == 401
