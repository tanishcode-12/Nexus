"""
Caching (Section 5).

Identical (tool, normalized-arguments) requests are cached for a
configurable TTL. Swappable backend via the CacheBackend interface:
- InMemoryCache: dict + TTL, dev/single-process default
- RedisCache: same interface, backed by a real Redis instance (production,
  multi-process). NOT exercised against a live Redis server in this
  environment (no network egress to a Redis instance here) — covered by a
  unit test using fakeredis instead. Treat as untested-against-real-Redis
  until you run it against yours; see README.
"""
from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod


class CacheBackend(ABC):
    @abstractmethod
    async def get(self, key: str) -> dict | None:
        ...

    @abstractmethod
    async def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        ...


def make_cache_key(tool_name: str, arguments: dict) -> str:
    """Hash of the normalized (tool, arguments) pair."""
    normalized = json.dumps(
        {"tool": tool_name, "arguments": arguments}, sort_keys=True, separators=(",", ":")
    )
    return "nexus:cache:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class InMemoryCache(CacheBackend):
    def __init__(self, default_ttl_seconds: int = 600):
        self.default_ttl_seconds = default_ttl_seconds
        self._store: dict[str, tuple[float, dict]] = {}  # key -> (expires_at, value)

    async def get(self, key: str) -> dict | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        self._store[key] = (time.monotonic() + ttl, value)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def size(self) -> int:
        return len(self._store)


class RedisCache(CacheBackend):
    """Requires `redis` (redis-py, asyncio client) at runtime.
    Import is deferred so the package is optional for in-memory-only setups.
    """

    def __init__(self, redis_url: str, default_ttl_seconds: int = 600):
        import redis.asyncio as redis  # type: ignore

        self.default_ttl_seconds = default_ttl_seconds
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def get(self, key: str) -> dict | None:
        raw = await self._redis.get(key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        await self._redis.set(key, json.dumps(value), ex=ttl)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)


def build_cache(backend: str, redis_url: str, ttl_seconds: int) -> CacheBackend:
    if backend == "redis":
        return RedisCache(redis_url, default_ttl_seconds=ttl_seconds)
    return InMemoryCache(default_ttl_seconds=ttl_seconds)
