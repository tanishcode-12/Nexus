from __future__ import annotations

import pytest

from auth import AuthStore, KeyRecord
from cache import InMemoryCache
from core import NexusServer
from omniroute_client import CompletionResult
from ratelimit import RateLimiter
from registry import ToolRegistry, ToolSpec
from registry import registry as global_registry
from tests.test_ask_model_tool import _FakeOmniRoute


def _registry_with(*names: str) -> ToolRegistry:
    """Borrow real ToolSpecs (handler + schema + scopes) out of the global
    registry into a fresh, isolated ToolRegistry — so tests exercise the
    *real* ask_model_v1 handler without any risk of cross-test pollution of
    global state."""
    reg = ToolRegistry()
    for name in names:
        reg.register(global_registry.get(name))
    return reg


async def _raising_handler(ctx, **kwargs):
    raise RuntimeError("boom - simulated bug in a tool handler")


async def _restricted_stub_handler(ctx, **kwargs):
    raise AssertionError("handler should never be invoked when scope check fails")


def _add_restricted_tool(reg: ToolRegistry) -> ToolRegistry:
    reg.register(
        ToolSpec(
            name="code_review_v1",
            description="restricted test tool",
            input_schema={"type": "object", "properties": {}},
            scopes=["code:review"],
            handler=_restricted_stub_handler,
        )
    )
    return reg


def _add_buggy_tool(reg: ToolRegistry) -> ToolRegistry:
    reg.register(
        ToolSpec(
            name="buggy_v1",
            description="always raises",
            input_schema={"type": "object", "properties": {}},
            scopes=[],
            handler=_raising_handler,
        )
    )
    return reg


@pytest.fixture
def fake_omniroute_inproc():
    return _FakeOmniRoute(
        result=CompletionResult(
            text="hi back", model="gpt-4o-mini", tokens_used=3, cost_estimate=0.0001, latency_ms=1.0
        )
    )


def _server(fake_omniroute_inproc, auth_store, rate_limiter, cache, metrics, extra_tools=()):
    reg = _registry_with("ask_model_v1")
    for adder in extra_tools:
        adder(reg)
    return NexusServer(
        omniroute=fake_omniroute_inproc,
        auth_store=auth_store,
        rate_limiter=rate_limiter,
        cache=cache,
        metrics=metrics,
        tool_registry=reg,
        discover=False,
    )


@pytest.mark.asyncio
async def test_unknown_tool(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics):
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics)
    result = await server.dispatch("does_not_exist_v1", {}, api_key="key-admin")
    assert result == {
        "error": True,
        "error_type": "unknown_tool",
        "message": "No such tool: does_not_exist_v1",
    }


@pytest.mark.asyncio
async def test_missing_api_key(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics):
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics)
    result = await server.dispatch("ask_model_v1", {"prompt": "hi"}, api_key="")
    assert result["error_type"] == "auth_error"


@pytest.mark.asyncio
async def test_unknown_api_key(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics):
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics)
    result = await server.dispatch("ask_model_v1", {"prompt": "hi"}, api_key="sk-totally-made-up")
    assert result["error_type"] == "auth_error"


@pytest.mark.asyncio
async def test_forbidden_scope_blocks_before_handler_runs(
    fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics
):
    server = _server(
        fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics,
        extra_tools=[_add_restricted_tool],
    )
    result = await server.dispatch("code_review_v1", {}, api_key="key-readonly")
    assert result["error_type"] == "forbidden_scope"
    assert "code:review" in result["message"]


@pytest.mark.asyncio
async def test_admin_scope_bypasses_restriction(
    fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics
):
    server = _server(
        fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics,
        extra_tools=[_add_restricted_tool],
    )
    result = await server.dispatch("code_review_v1", {}, api_key="key-admin")
    # admin bypasses scoping, so it reaches the handler, which raises -> internal_error,
    # NOT forbidden_scope. That distinction is the point of this test.
    assert result["error_type"] == "internal_error"


@pytest.mark.asyncio
async def test_rate_limit_enforced_per_key(fake_omniroute_inproc, metrics, memory_cache, test_config):
    auth_store = AuthStore(
        records=[
            KeyRecord(
                api_key="tight",
                scopes=["model:read"],
                rate_capacity=2,
                rate_refill_per_sec=0.0,
                daily_quota=1000,
                monthly_quota=1000,
            )
        ]
    )
    rate_limiter = RateLimiter(test_config, auth_store)
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics)

    r1 = await server.dispatch("ask_model_v1", {"prompt": "one"}, api_key="tight")
    r2 = await server.dispatch("ask_model_v1", {"prompt": "two"}, api_key="tight")
    r3 = await server.dispatch("ask_model_v1", {"prompt": "three"}, api_key="tight")
    assert r1["error"] is False and r2["error"] is False
    assert r3["error_type"] == "rate_limited"


@pytest.mark.asyncio
async def test_cache_hit_skips_second_omniroute_call(
    fake_omniroute_inproc, auth_store, rate_limiter, metrics
):
    cache = InMemoryCache(default_ttl_seconds=60)
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, cache, metrics)
    args = {"prompt": "cache me please", "model": "gpt-4o-mini"}

    r1 = await server.dispatch("ask_model_v1", args, api_key="key-admin")
    r2 = await server.dispatch("ask_model_v1", args, api_key="key-admin")

    assert r1.get("cache_hit") is None  # first call: not from cache
    assert r2.get("cache_hit") is True
    assert len(fake_omniroute_inproc.calls) == 1  # OmniRoute only ever hit once


@pytest.mark.asyncio
async def test_bad_arguments_returns_structured_error(
    fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics
):
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics)
    # ask_model_v1 doesn't accept `unexpected_field` -> TypeError inside the handler call
    result = await server.dispatch("ask_model_v1", {"unexpected_field": 1}, api_key="key-admin")
    assert result["error_type"] == "bad_arguments"


@pytest.mark.asyncio
async def test_dispatch_logs_real_token_usage_not_zero(
    fake_omniroute_inproc, auth_store, rate_limiter, memory_cache
):
    """Regression test: _log() used to silently drop tokens_used/cost_estimate
    even though tool results carry them (a real gap caught during Section 7
    review, not by any test — this test exists so it can't regress silently
    again)."""
    recorded = []

    class _SpyMetrics:
        def record(self, **kwargs):
            recorded.append(kwargs)

    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, _SpyMetrics())
    await server.dispatch("ask_model_v1", {"prompt": "hi"}, api_key="key-admin")

    assert len(recorded) == 1
    assert recorded[0]["tokens_used"] == 3  # from fake_omniroute_inproc's canned CompletionResult
    assert recorded[0]["cost_estimate"] == pytest.approx(0.0001)


@pytest.mark.asyncio
async def test_cache_hit_logs_zero_usage(fake_omniroute_inproc, auth_store, rate_limiter):
    """A cache hit didn't consume any new tokens, so it should log zero —
    not the original call's token count."""
    recorded = []

    class _SpyMetrics:
        def record(self, **kwargs):
            recorded.append(kwargs)

    cache = InMemoryCache(default_ttl_seconds=60)
    server = _server(fake_omniroute_inproc, auth_store, rate_limiter, cache, _SpyMetrics())
    args = {"prompt": "cache me", "model": "gpt-4o-mini"}
    await server.dispatch("ask_model_v1", args, api_key="key-admin")
    await server.dispatch("ask_model_v1", args, api_key="key-admin")

    assert len(recorded) == 2
    assert recorded[0]["tokens_used"] == 3       # real call
    assert recorded[1]["tokens_used"] == 0       # cache hit: no new tokens spent
    assert recorded[1]["cache_hit"] is True


@pytest.mark.asyncio
async def test_unhandled_exception_never_escapes_dispatch(
    fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics
):
    server = _server(
        fake_omniroute_inproc, auth_store, rate_limiter, memory_cache, metrics,
        extra_tools=[_add_buggy_tool],
    )
    # Section 8: must not raise out of dispatch(), must come back as a clean error dict
    result = await server.dispatch("buggy_v1", {}, api_key="key-admin")
    assert result == {
        "error": True,
        "error_type": "internal_error",
        "message": "An internal error occurred.",
    }
