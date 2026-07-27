from __future__ import annotations

import pytest

from auth import AuthStore, KeyRecord
from cache import InMemoryCache
from config import Config
from context import RequestContext
from core import NexusServer
from http_main import build_app
from metrics import MetricsRecorder
from omniroute_client import CompletionResult, OmniRouteClient, OmniRouteError
from ratelimit import RateLimiter
from registry import ToolRegistry
from registry import registry as global_registry
from tools.code_review import code_review_v1
from tools.research_chain import research_chain_v1


class _SequentialFakeOmniRoute:
    """Returns canned results/errors in call order — needed here because
    research_chain/code_review always call complete() with model=None, so a
    dict-keyed-by-model fake (used elsewhere) can't tell the calls apart."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[str] = []

    async def complete(self, prompt, model=None, max_tokens=1024):
        self.calls.append(prompt)
        outcome = self._results.pop(0)
        if isinstance(outcome, OmniRouteError):
            raise outcome
        return outcome


def _ok(text, model="m"):
    return CompletionResult(text=text, model=model, tokens_used=5, cost_estimate=0.001, latency_ms=1.0)


def _ctx(fake):
    return RequestContext(api_key="k", scopes=["model:read", "code:review"], omniroute=fake)


# ---- research_chain_v1 unit tests ----


@pytest.mark.asyncio
async def test_research_chain_streams_research_then_synthesis():
    fake = _SequentialFakeOmniRoute([_ok("key facts here"), _ok("final synthesis here")])
    chunks = [c async for c in research_chain_v1(topic="black holes", ctx=_ctx(fake))]
    assert len(chunks) == 2
    assert chunks[0]["phase"] == "research" and chunks[0]["partial"] is True
    assert chunks[0]["text"] == "key facts here"
    assert chunks[1]["phase"] == "synthesis" and chunks[1]["partial"] is False
    assert chunks[1]["text"] == "final synthesis here"
    assert "black holes" in fake.calls[0]


@pytest.mark.asyncio
async def test_research_chain_stops_if_research_phase_fails():
    fake = _SequentialFakeOmniRoute([OmniRouteError("down", error_type="upstream_unavailable")])
    chunks = [c async for c in research_chain_v1(topic="x", ctx=_ctx(fake))]
    assert len(chunks) == 1
    assert chunks[0]["error"] is True and chunks[0]["phase"] == "research"
    assert len(fake.calls) == 1  # synthesis was never attempted


@pytest.mark.asyncio
async def test_research_chain_synthesis_failure_after_research_succeeds():
    fake = _SequentialFakeOmniRoute(
        [_ok("research ok"), OmniRouteError("down", error_type="upstream_unavailable")]
    )
    chunks = [c async for c in research_chain_v1(topic="x", ctx=_ctx(fake))]
    assert len(chunks) == 2
    assert chunks[0]["error"] is False
    assert chunks[1]["error"] is True and chunks[1]["phase"] == "synthesis"


# ---- code_review_v1 unit tests ----


@pytest.mark.asyncio
async def test_code_review_small_input_single_chunk_no_summary():
    fake = _SequentialFakeOmniRoute([_ok("looks fine, minor nit on naming")])
    chunks = [c async for c in code_review_v1(snippet="x = 1\ny = 2", language="python", ctx=_ctx(fake))]
    assert len(chunks) == 1
    assert chunks[0]["partial"] is False
    assert chunks[0]["total_chunks"] == 1
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_code_review_large_input_chunks_then_summarizes():
    big_snippet = "\n".join(f"x{i} = {i}" for i in range(100))  # 100 lines -> 3 chunks of 40/40/20
    fake = _SequentialFakeOmniRoute(
        [_ok("chunk0 review"), _ok("chunk1 review"), _ok("chunk2 review"), _ok("overall summary")]
    )
    chunks = [c async for c in code_review_v1(snippet=big_snippet, language="python", ctx=_ctx(fake))]
    assert len(chunks) == 4  # 3 chunk reviews + 1 summary
    assert all(c["partial"] is True for c in chunks[:3])
    assert chunks[3]["partial"] is False and chunks[3]["phase"] == "summary"
    assert chunks[3]["text"] == "overall summary"
    assert len(fake.calls) == 4


@pytest.mark.asyncio
async def test_code_review_stops_cleanly_on_mid_chunk_failure():
    lines_50 = "\n".join(f"x{i}=1" for i in range(50))  # -> 2 chunks (40 + 10)
    fake = _SequentialFakeOmniRoute(
        [_ok("chunk0 review"), OmniRouteError("down", error_type="upstream_unavailable")]
    )
    chunks = [c async for c in code_review_v1(snippet=lines_50, language="python", ctx=_ctx(fake))]
    assert len(chunks) == 2
    assert chunks[0]["error"] is False
    assert chunks[1]["error"] is True and chunks[1]["chunk_index"] == 1


@pytest.mark.asyncio
async def test_code_review_summary_failure_after_all_chunks_succeed():
    lines_50 = "\n".join(f"x{i}=1" for i in range(50))
    fake = _SequentialFakeOmniRoute(
        [_ok("chunk0"), _ok("chunk1"), OmniRouteError("down", error_type="upstream_unavailable")]
    )
    chunks = [c async for c in code_review_v1(snippet=lines_50, language="python", ctx=_ctx(fake))]
    assert len(chunks) == 3
    assert chunks[2]["error"] is True and chunks[2]["phase"] == "summary"


# ---- real end-to-end SSE tests: real Flask app, real fake-OmniRoute HTTP server ----


def _e2e_app(fake_omniroute, tmp_path):
    reg = ToolRegistry()
    for name in ("research_chain_v1", "code_review_v1"):
        reg.register(global_registry.get(name))
    auth_store = AuthStore(records=[KeyRecord(api_key="key-admin", scopes=["admin"])])
    cfg = Config()
    cfg.db_path = str(tmp_path / "streaming_e2e_quota.sqlite3")
    server = NexusServer(
        omniroute=OmniRouteClient(base_url=fake_omniroute.base_url, api_key="k", max_retries=1, backoff_base_seconds=0.01),
        auth_store=auth_store,
        rate_limiter=RateLimiter(cfg, auth_store),
        cache=InMemoryCache(),
        metrics=MetricsRecorder(),
        tool_registry=reg,
        discover=False,
    )
    return build_app(server, auth_store).test_client()


def test_research_chain_e2e_sse_over_real_flask_and_real_http(fake_omniroute, tmp_path):
    client = _e2e_app(fake_omniroute, tmp_path)
    resp = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-admin", "Accept": "text/event-stream"},
        json={"tool": "research_chain_v1", "arguments": {"topic": "quantum computing"}},
    )
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/event-stream")
    body = resp.get_data(as_text=True)
    assert '"phase": "research"' in body
    assert '"phase": "synthesis"' in body
    assert "quantum computing" in body  # the fake server echoes the prompt back
    assert body.strip().endswith("event: done\ndata: {}")


def test_code_review_e2e_sse_large_input_over_real_flask(fake_omniroute, tmp_path):
    client = _e2e_app(fake_omniroute, tmp_path)
    big_snippet = "\n".join(f"def f{i}(): return {i}" for i in range(90))
    resp = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-admin", "Accept": "text/event-stream"},
        json={"tool": "code_review_v1", "arguments": {"snippet": big_snippet, "language": "python"}},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert body.count('"chunk_index"') == 3  # 90 lines -> 3 chunks (40/40/10)
    assert '"phase": "summary"' in body
    assert body.strip().endswith("event: done\ndata: {}")
