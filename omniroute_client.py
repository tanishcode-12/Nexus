"""
Thin async client for OmniRoute's unified LLM API.

OmniRoute is assumed to expose an OpenAI-compatible-ish surface:
  POST {base_url}/v1/chat/completions   { model, messages, ... }
  GET  {base_url}/v1/models

This module is deliberately the *only* place that knows OmniRoute's wire
format. Tools never call httpx directly — they call OmniRouteClient methods,
so retry/backoff/fallback logic (Section 8) lives in exactly one place.

NOTE: this has NOT been exercised against a real OmniRoute instance from
this environment — the sandbox this was built in only has network egress to
a fixed allowlist (pypi, github, api.anthropic.com, etc.), not to a
localhost/LAN OmniRoute gateway. It is covered by unit tests using a fake
transport instead (see tests/test_omniroute_client.py). You should run a
smoke test against your real OmniRoute endpoint before trusting this in
production — see README "What's verified vs. not".
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

logger = logging.getLogger("nexus.omniroute")


class OmniRouteError(Exception):
    """Raised when OmniRoute (and any configured fallback) both fail."""

    def __init__(self, message: str, error_type: str = "upstream_error"):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class CompletionResult:
    text: str
    model: str
    tokens_used: int
    cost_estimate: float
    latency_ms: float


class OmniRouteClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str = "gpt-4o-mini",
        fallback_model: str | None = None,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.5,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.fallback_model = fallback_model
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,  # injected fake transport in tests
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.get("/v1/models")
                resp.raise_for_status()
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_base_seconds * (2 ** attempt))
                    continue
        raise OmniRouteError(
            f"OmniRoute list_models failed after {self.max_retries + 1} attempts: {last_exc}",
            error_type="upstream_unavailable",
        )

    async def complete(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> CompletionResult:
        """Single completion with retry+backoff, then model fallback."""
        chosen_model = model or self.default_model
        try:
            return await self._complete_with_retry(prompt, chosen_model, max_tokens)
        except OmniRouteError:
            if self.fallback_model and self.fallback_model != chosen_model:
                logger.warning(
                    "omniroute.fallback",
                    extra={"from_model": chosen_model, "to_model": self.fallback_model},
                )
                return await self._complete_with_retry(
                    prompt, self.fallback_model, max_tokens
                )
            raise

    async def _complete_with_retry(
        self, prompt: str, model: str, max_tokens: int
    ) -> CompletionResult:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            start = time.monotonic()
            try:
                resp = await self._client.post(
                    "/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                latency_ms = (time.monotonic() - start) * 1000
                usage = data.get("usage", {})
                tokens_used = usage.get("total_tokens", 0)
                return CompletionResult(
                    text=data["choices"][0]["message"]["content"],
                    model=model,
                    tokens_used=tokens_used,
                    cost_estimate=_estimate_cost(model, tokens_used),
                    latency_ms=latency_ms,
                )
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self.backoff_base_seconds * (2 ** attempt)
                    logger.warning(
                        "omniroute.retry",
                        extra={"attempt": attempt, "model": model, "delay": delay},
                    )
                    await asyncio.sleep(delay)
                    continue
        raise OmniRouteError(
            f"OmniRoute request failed after {self.max_retries + 1} attempts: {last_exc}",
            error_type="upstream_unavailable",
        )

    async def stream_complete(
        self, prompt: str, model: str | None = None
    ) -> AsyncIterator[str]:
        """Yield partial text chunks. Used by streaming tools (Section 6)."""
        chosen_model = model or self.default_model
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": chosen_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                yield payload


# Rough $/1K-token estimates for cost tracking in logs — not billing-accurate,
# just enough for the observability layer to show relative cost. Update
# per your OmniRoute pricing config.
_COST_PER_1K_TOKENS = {
    "gpt-4o-mini": 0.00015,
    "gpt-4o": 0.005,
    "claude-sonnet-5": 0.003,
}


def _estimate_cost(model: str, tokens_used: int) -> float:
    rate = _COST_PER_1K_TOKENS.get(model, 0.001)
    return round((tokens_used / 1000) * rate, 6)
