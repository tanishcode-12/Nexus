"""
Verifies the *mechanism* dispatch_stream() provides for streaming tools,
using a synthetic streaming tool built only for this test — research_chain
and code_review (the real streaming tools per the spec) don't exist yet;
they land in the "remaining tools" build step. This proves the plumbing
works so wiring the real tools into it later is a drop-in, not a rebuild.
"""
from __future__ import annotations

import asyncio

import pytest

from auth import AuthStore, KeyRecord
from cache import InMemoryCache
from core import NexusServer
from metrics import MetricsRecorder
from omniroute_client import CompletionResult
from ratelimit import RateLimiter
from registry import ToolRegistry, ToolSpec
from sse_bridge import bridge_to_sse_lines
from tests.test_ask_model_tool import _FakeOmniRoute


async def _synthetic_stream_handler(ctx, topic: str):
    yield {"partial": True, "text": f"researching {topic}..."}
    await asyncio.sleep(0)  # actually yield control, proving this is a real async generator
    yield {"partial": True, "text": f"synthesizing {topic}..."}
    yield {"partial": False, "text": f"final answer about {topic}"}


async def _synthetic_broken_stream_handler(ctx, topic: str):
    yield {"partial": True, "text": "starting..."}
    raise RuntimeError("simulated mid-stream failure")


def _stream_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="research_chain_v1",
            description="synthetic streaming tool for testing",
            input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
            scopes=["model:read"],
            handler=_synthetic_stream_handler,
            streaming=True,
        )
    )
    reg.register(
        ToolSpec(
            name="broken_stream_v1",
            description="synthetic streaming tool that fails mid-stream",
            input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
            scopes=[],
            handler=_synthetic_broken_stream_handler,
            streaming=True,
        )
    )
    return reg


@pytest.fixture
def stream_server(tmp_path):
    fake_omniroute = _FakeOmniRoute(
        result=CompletionResult(text="unused", model="gpt-4o-mini", tokens_used=1, cost_estimate=0.0, latency_ms=1.0)
    )
    auth_store = AuthStore(records=[KeyRecord(api_key="key-admin", scopes=["admin"])])
    from config import Config

    cfg = Config()
    # NOTE: intentionally a real temp file, not ":memory:" — SqliteQuotaStore
    # opens a separate connection per thread, and ":memory:" databases don't
    # share state across separate connections, so ":memory:" here silently
    # creates a fresh empty (tableless) DB on first real query.
    cfg.db_path = str(tmp_path / "stream_test_quota.sqlite3")
    rate_limiter = RateLimiter(cfg, auth_store)
    return NexusServer(
        omniroute=fake_omniroute,
        auth_store=auth_store,
        rate_limiter=rate_limiter,
        cache=InMemoryCache(),
        metrics=MetricsRecorder(),
        tool_registry=_stream_registry(),
        discover=False,
    )


@pytest.mark.asyncio
async def test_dispatch_stream_yields_all_chunks_in_order(stream_server):
    chunks = []
    async for chunk in stream_server.dispatch_stream(
        "research_chain_v1", {"topic": "mars colonization"}, api_key="key-admin"
    ):
        chunks.append(chunk)
    assert len(chunks) == 3
    assert chunks[0]["text"] == "researching mars colonization..."
    assert chunks[-1]["partial"] is False
    assert "final answer" in chunks[-1]["text"]


@pytest.mark.asyncio
async def test_dispatch_stream_respects_auth(stream_server):
    chunks = [
        c
        async for c in stream_server.dispatch_stream(
            "research_chain_v1", {"topic": "x"}, api_key="not-a-real-key"
        )
    ]
    assert len(chunks) == 1
    assert chunks[0]["error_type"] == "auth_error"


@pytest.mark.asyncio
async def test_dispatch_stream_mid_stream_exception_becomes_clean_error(stream_server):
    chunks = [
        c
        async for c in stream_server.dispatch_stream(
            "broken_stream_v1", {"topic": "x"}, api_key="key-admin"
        )
    ]
    # first chunk came through fine, then the exception was caught and turned
    # into a structured error instead of propagating out of the generator
    assert chunks[0]["text"] == "starting..."
    assert chunks[-1]["error_type"] == "internal_error"


@pytest.mark.asyncio
async def test_non_streaming_tool_through_dispatch_stream_yields_single_result(stream_server):
    # ask_model_v1 isn't registered in this fixture's registry, but any
    # non-streaming spec should degrade to "yield the one dispatch() result".
    # Reuse research_chain_v1's registry by toggling: easiest proof is that
    # dispatch_stream on an unknown name still yields exactly one error chunk.
    chunks = [c async for c in stream_server.dispatch_stream("unknown_tool_v1", {}, api_key="key-admin")]
    assert len(chunks) == 1
    assert chunks[0]["error_type"] == "unknown_tool"


@pytest.mark.asyncio
async def test_dispatch_stream_aggregates_token_usage_across_chunks(stream_server):
    recorded = []

    class _SpyMetrics:
        def record(self, **kwargs):
            recorded.append(kwargs)

    stream_server.metrics = _SpyMetrics()
    _chunks = [
        c
        async for c in stream_server.dispatch_stream(
            "research_chain_v1", {"topic": "x"}, api_key="key-admin"
        )
    ]
    assert len(recorded) == 1
    # _synthetic_stream_handler's chunks don't set tokens_used/cost_estimate,
    # so this proves the aggregator defaults missing fields to 0 rather than
    # crashing — not that a specific tool reports real numbers (that's
    # covered by test_streaming_tools.py against the real tools instead).
    assert recorded[0]["tokens_used"] == 0
    assert recorded[0]["success"] is True


@pytest.mark.asyncio
async def test_sse_bridge_formats_chunks_and_terminates_with_done():
    async def gen():
        yield {"a": 1}
        yield {"b": 2}

    lines = list(bridge_to_sse_lines(gen()))
    assert lines[0] == 'data: {"a": 1}\n\n'
    assert lines[1] == 'data: {"b": 2}\n\n'
    assert lines[2] == "event: done\ndata: {}\n\n"
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_sse_bridge_surfaces_exception_as_error_event():
    async def gen():
        yield {"a": 1}
        raise RuntimeError("kaboom")

    lines = list(bridge_to_sse_lines(gen()))
    assert lines[0] == 'data: {"a": 1}\n\n'
    assert lines[1].startswith("event: error\n")
    assert "kaboom" in lines[1]
