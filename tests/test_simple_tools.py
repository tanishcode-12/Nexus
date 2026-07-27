from __future__ import annotations

import pytest

from context import RequestContext
from omniroute_client import CompletionResult, OmniRouteError
from tools.compare_models import compare_models_v1
from tools.list_available_models import list_available_models_v1
from tools.summarize_text import summarize_text_v1


class _FakeOmniRoute:
    """Configurable per-model behavior: dict of model -> CompletionResult or
    OmniRouteError, plus a bare list for list_models()."""

    def __init__(self, by_model=None, models_list=None, models_error=None):
        self.by_model = by_model or {}
        self.models_list = models_list or []
        self.models_error = models_error
        self.calls = []

    async def complete(self, prompt, model=None, max_tokens=1024):
        self.calls.append(model)
        outcome = self.by_model.get(model)
        if isinstance(outcome, OmniRouteError):
            raise outcome
        return outcome

    async def list_models(self):
        if self.models_error is not None:
            raise self.models_error
        return self.models_list


def _ctx(fake):
    return RequestContext(api_key="k", scopes=["model:read"], omniroute=fake)


def _ok(text, model="m", tokens=5):
    return CompletionResult(text=text, model=model, tokens_used=tokens, cost_estimate=0.001, latency_ms=1.0)


# ---- compare_models_v1 ----


@pytest.mark.asyncio
async def test_compare_models_all_succeed():
    fake = _FakeOmniRoute(by_model={"a": _ok("resp-a", "a"), "b": _ok("resp-b", "b")})
    result = await compare_models_v1(prompt="p", models=["a", "b"], ctx=_ctx(fake))
    assert result["error"] is False
    assert result["succeeded"] == 2
    assert result["failed"] == 0
    texts = {r["model"]: r["text"] for r in result["results"]}
    assert texts == {"a": "resp-a", "b": "resp-b"}


@pytest.mark.asyncio
async def test_compare_models_partial_failure_is_not_top_level_error():
    fake = _FakeOmniRoute(
        by_model={
            "a": _ok("resp-a", "a"),
            "b": OmniRouteError("down", error_type="upstream_unavailable"),
        }
    )
    result = await compare_models_v1(prompt="p", models=["a", "b"], ctx=_ctx(fake))
    assert result["error"] is False  # partial failure isn't total failure
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    failed_entry = next(r for r in result["results"] if r["model"] == "b")
    assert failed_entry["error"] is True


@pytest.mark.asyncio
async def test_compare_models_total_failure_is_top_level_error():
    err = OmniRouteError("down", error_type="upstream_unavailable")
    fake = _FakeOmniRoute(by_model={"a": err, "b": err})
    result = await compare_models_v1(prompt="p", models=["a", "b"], ctx=_ctx(fake))
    assert result["error"] is True
    assert result["error_type"] == "upstream_unavailable"


# ---- summarize_text_v1 ----


@pytest.mark.asyncio
async def test_summarize_text_under_budget_not_truncated():
    fake = _FakeOmniRoute(by_model={None: _ok("short summary here")})
    result = await summarize_text_v1(text="a" * 500, max_words=10, ctx=_ctx(fake))
    assert result["error"] is False
    assert result["truncated_by_nexus"] is False
    assert result["summary"] == "short summary here"


@pytest.mark.asyncio
async def test_summarize_text_over_budget_gets_truncated():
    long_summary = " ".join(f"word{i}" for i in range(50))
    fake = _FakeOmniRoute(by_model={None: _ok(long_summary)})
    result = await summarize_text_v1(text="a" * 5000, max_words=5, ctx=_ctx(fake))
    assert result["error"] is False
    assert result["truncated_by_nexus"] is True
    assert result["word_count"] == 5
    assert result["summary"] == "word0 word1 word2 word3 word4"


@pytest.mark.asyncio
async def test_summarize_text_upstream_error_propagates_as_structured_error():
    fake = _FakeOmniRoute(by_model={None: OmniRouteError("nope", error_type="upstream_unavailable")})
    result = await summarize_text_v1(text="x", max_words=10, ctx=_ctx(fake))
    assert result["error"] is True
    assert result["error_type"] == "upstream_unavailable"


# ---- list_available_models_v1 ----


@pytest.mark.asyncio
async def test_list_available_models_happy_path():
    fake = _FakeOmniRoute(models_list=["gpt-4o-mini", "gpt-4o", "claude-sonnet-5"])
    result = await list_available_models_v1(ctx=_ctx(fake))
    assert result["error"] is False
    assert result["count"] == 3
    assert "claude-sonnet-5" in result["models"]


@pytest.mark.asyncio
async def test_list_available_models_upstream_error():
    fake = _FakeOmniRoute(models_error=OmniRouteError("down", error_type="upstream_unavailable"))
    result = await list_available_models_v1(ctx=_ctx(fake))
    assert result["error"] is True
    assert result["error_type"] == "upstream_unavailable"
