from __future__ import annotations

import asyncio

import pytest

from cache import InMemoryCache, RedisCache, make_cache_key


def test_make_cache_key_stable_for_identical_input():
    k1 = make_cache_key("ask_model_v1", {"prompt": "hi", "model": "gpt-4o-mini"})
    k2 = make_cache_key("ask_model_v1", {"model": "gpt-4o-mini", "prompt": "hi"})  # different order
    assert k1 == k2  # normalized (sorted keys), so key order shouldn't matter


def test_make_cache_key_differs_for_different_input():
    k1 = make_cache_key("ask_model_v1", {"prompt": "hi", "model": "gpt-4o-mini"})
    k2 = make_cache_key("ask_model_v1", {"prompt": "bye", "model": "gpt-4o-mini"})
    assert k1 != k2


@pytest.mark.asyncio
async def test_inmemory_cache_get_set_roundtrip():
    cache = InMemoryCache(default_ttl_seconds=60)
    assert await cache.get("missing") is None
    await cache.set("k", {"text": "cached value"})
    assert await cache.get("k") == {"text": "cached value"}


@pytest.mark.asyncio
async def test_inmemory_cache_expires_after_ttl():
    cache = InMemoryCache(default_ttl_seconds=0)  # instant expiry
    await cache.set("k", {"text": "gone soon"}, ttl_seconds=0)
    await asyncio.sleep(0.01)
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_inmemory_cache_delete():
    cache = InMemoryCache(default_ttl_seconds=60)
    await cache.set("k", {"v": 1})
    await cache.delete("k")
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_redis_cache_roundtrip_via_fakeredis(monkeypatch):
    import fakeredis

    def fake_from_url(url, decode_responses=True):
        return fakeredis.FakeAsyncRedis(decode_responses=decode_responses)

    monkeypatch.setattr("redis.asyncio.from_url", fake_from_url)

    cache = RedisCache("redis://ignored:6379/0", default_ttl_seconds=60)
    assert await cache.get("missing") is None
    await cache.set("k", {"text": "from redis"})
    assert await cache.get("k") == {"text": "from redis"}
    await cache.delete("k")
    assert await cache.get("k") is None
