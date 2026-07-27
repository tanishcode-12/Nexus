from __future__ import annotations

import json

import pytest

from auth import AuthError, AuthStore, KeyRecord


def test_scopes_for_known_key(auth_store):
    assert auth_store.scopes_for("key-readonly") == ["model:read"]


def test_scopes_for_missing_key_raises(auth_store):
    with pytest.raises(AuthError):
        auth_store.scopes_for("")


def test_scopes_for_unknown_key_raises(auth_store):
    with pytest.raises(AuthError):
        auth_store.scopes_for("sk-not-a-real-key")


def test_add_key_at_runtime(auth_store):
    auth_store.add_key(KeyRecord(api_key="key-new", scopes=["code:review"]))
    assert auth_store.scopes_for("key-new") == ["code:review"]


def test_load_from_json_file(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"api_key": "sk-a", "scopes": ["model:read"]},
                    {"api_key": "sk-b", "scopes": ["model:read", "code:review"], "daily_quota": 10},
                ]
            }
        )
    )
    store = AuthStore(keys_file=str(keys_file))
    assert store.scopes_for("sk-a") == ["model:read"]
    rec = store.record_for("sk-b")
    assert rec.daily_quota == 10
    assert "code:review" in rec.scopes


def test_missing_keys_file_is_not_an_error(tmp_path):
    store = AuthStore(keys_file=str(tmp_path / "does_not_exist.json"))
    with pytest.raises(AuthError):
        store.scopes_for("anything")
