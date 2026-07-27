"""
Auth & scoping (Section 3).

Each API key maps to a set of scopes. Scope enforcement itself happens
centrally in core.NexusServer.dispatch — this module is just the source of
truth for "what scopes does this key have", backed by a JSON file for
simplicity (swap for a DB table if you need rotation/expiry/audit at scale;
see README tradeoffs).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


class AuthError(Exception):
    pass


@dataclass
class KeyRecord:
    api_key: str
    scopes: list[str]
    rate_capacity: int | None = None       # per-key override, Stage 4
    rate_refill_per_sec: float | None = None
    daily_quota: int | None = None
    monthly_quota: int | None = None


class AuthStore:
    """In-memory store loaded from a JSON keys file.

    File format:
    {
      "keys": [
        {"api_key": "sk-nexus-...", "scopes": ["model:read", "code:review"]},
        {"api_key": "sk-nexus-readonly-...", "scopes": ["model:read"]}
      ]
    }
    """

    def __init__(self, keys_file: str | None = None, records: list[KeyRecord] | None = None):
        self._by_key: dict[str, KeyRecord] = {}
        if records:
            for r in records:
                self._by_key[r.api_key] = r
        if keys_file and os.path.exists(keys_file):
            self._load(keys_file)

    def _load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for entry in data.get("keys", []):
            rec = KeyRecord(
                api_key=entry["api_key"],
                scopes=entry.get("scopes", []),
                rate_capacity=entry.get("rate_capacity"),
                rate_refill_per_sec=entry.get("rate_refill_per_sec"),
                daily_quota=entry.get("daily_quota"),
                monthly_quota=entry.get("monthly_quota"),
            )
            self._by_key[rec.api_key] = rec

    def scopes_for(self, api_key: str) -> list[str]:
        if not api_key:
            raise AuthError("Missing API key.")
        rec = self._by_key.get(api_key)
        if rec is None:
            raise AuthError("Unknown API key.")
        return rec.scopes

    def record_for(self, api_key: str) -> KeyRecord | None:
        return self._by_key.get(api_key)

    def add_key(self, record: KeyRecord) -> None:
        self._by_key[record.api_key] = record
