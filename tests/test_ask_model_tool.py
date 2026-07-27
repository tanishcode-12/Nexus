from __future__ import annotations

import pytest

from context import RequestContext
from omniroute_client import CompletionResult, OmniRouteError
from tools.ask_model import ask_model_v1


class _FakeOmniRoute:
    """No real HTTP call at all — pure in-memory fake, for pure unit testing
    of the tool handler in isolation from OmniRouteClient/httpx entirely."""

    def __init__(self, result=None, error: OmniRouteError | None = None):
        self._result = result
        self._error = error
        self.calls = []

    async def complete(self, prompt, model=None, max_tokens=1024):
        self.calls.append({"prompt": prompt, "model": model})
        if self._error is not None:
            raise self._error
        return self._result


def _ctx(fake_omniroute) -> RequestContext:
    return RequestContext(api_key="k", scopes=["model:read"], omniroute=fake_omniroute)


@pytest.mark.asyncio
async def test_ask_model_v1_happy_path():
    fake = _FakeOmniRoute(
        result=CompletionResult(
            text="hello!", model="gpt-4o-mini", tokens_used=5, cost_estimate=0.0001, latency_ms=12.3
        )
    )
    result = await ask_model_v1(prompt="hi", ctx=_ctx(fake))
    assert result["error"] is False
    assert result["text"] == "hello!"
    assert result["model"] == "gpt-4o-mini"
    assert fake.calls == [{"prompt": "hi", "model": None}]


@pytest.mark.asyncio
async def test_ask_model_v1_passes_model_through():
    fake = _FakeOmniRoute(
        result=CompletionResult(
            text="x", model="gpt-4o", tokens_used=1, cost_estimate=0.0, latency_ms=1.0
        )
    )
    await ask_model_v1(prompt="hi", ctx=_ctx(fake), model="gpt-4o")
    assert fake.calls == [{"prompt": "hi", "model": "gpt-4o"}]


@pytest.mark.asyncio
async def test_ask_model_v1_surfaces_omniroute_error_as_structured_result():
    fake = _FakeOmniRoute(error=OmniRouteError("upstream is down", error_type="upstream_unavailable"))
    result = await ask_model_v1(prompt="hi", ctx=_ctx(fake))
    assert result["error"] is True
    assert result["error_type"] == "upstream_unavailable"
    assert "upstream is down" in result["message"]
