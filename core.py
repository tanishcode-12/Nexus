"""
Core Nexus server: one mcp.server.lowlevel.Server instance with tool
list/call wired to the plugin registry. Transport-specific adapters
(stdio_main.py, http_main.py) just plug this instance into a transport —
no tool logic is duplicated per transport.

Pipeline for every tool call (grows through the build stages):
  Stage 1: auth -> execute -> return
  Stage 3: + scope check
  Stage 4: + rate limit / quota check
  Stage 5: + cache check
  Stage 7: + structured logging / metrics
  Stage 8: + retry/fallback (lives inside OmniRouteClient, not here)
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

from mcp import types
from mcp.server.lowlevel import Server

from auth import AuthError, AuthStore
from cache import CacheBackend, make_cache_key
from context import RequestContext
from metrics import MetricsRecorder
from omniroute_client import OmniRouteClient
from ratelimit import QuotaExceeded, RateLimiter
from registry import ToolRegistry, discover_tools
from registry import registry as global_registry

logger = logging.getLogger("nexus.core")


class NexusServer:
    def __init__(
        self,
        omniroute: OmniRouteClient,
        auth_store: AuthStore | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: CacheBackend | None = None,
        metrics: MetricsRecorder | None = None,
        tool_registry: ToolRegistry | None = None,
        discover: bool = True,
    ):
        self.omniroute = omniroute
        self.auth_store = auth_store
        self.rate_limiter = rate_limiter
        self.cache = cache
        self.metrics = metrics
        # Defaults to the process-wide registry; tests can inject a fresh
        # ToolRegistry (with discover=False) to register fake tools without
        # touching the real tools/ package.
        self.registry = tool_registry if tool_registry is not None else global_registry
        self.mcp_server = Server("nexus")
        if discover:
            discover_tools("tools")
        self._wire_handlers()

    def _wire_handlers(self) -> None:
        @self.mcp_server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=spec.input_schema,
                )
                for spec in self.registry.all()
                if not spec.deprecated
            ]

        @self.mcp_server.call_tool()
        async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            result = await self.dispatch(name, arguments, api_key=self._current_api_key())
            return [types.TextContent(type="text", text=json.dumps(result))]

    def _current_api_key(self) -> str:
        # For stdio, the key comes from an env var set once at process start
        # (see stdio_main.py). For HTTP, dispatch() is called directly by the
        # transport adapter with the key parsed from the request header, so
        # this fallback path is stdio-only.
        import os

        return os.environ.get("NEXUS_API_KEY", "")

    async def _check_auth_and_scope(self, tool_name: str, spec, api_key: str) -> tuple[list[str] | None, dict | None]:
        """Shared by dispatch() and dispatch_stream(). Returns (scopes, None)
        on success or (None, error_dict) on failure — never both non-None."""
        try:
            scopes = self.auth_store.scopes_for(api_key) if self.auth_store else ["admin"]
        except AuthError as exc:
            return None, self._error("auth_error", str(exc))

        if not (spec.scopes == [] or any(s in scopes or "admin" in scopes for s in spec.scopes)):
            return None, self._error(
                "forbidden_scope",
                f"API key lacks required scope(s) {spec.scopes} for tool {tool_name}",
            )
        return scopes, None

    async def dispatch(self, tool_name: str, arguments: dict, api_key: str) -> dict:
        """The single entry point both transports funnel non-streaming
        tool calls through."""
        request_id = uuid.uuid4().hex[:12]
        start = time.monotonic()
        spec = self.registry.get(tool_name)

        if spec is None:
            return self._error("unknown_tool", f"No such tool: {tool_name}")

        # --- Auth & scoping (Stage 3) ---
        scopes, auth_err = await self._check_auth_and_scope(tool_name, spec, api_key)
        if auth_err is not None:
            self._log(tool_name, api_key, start, False, auth_err["error_type"])
            return auth_err

        # --- Rate limit & quota (Stage 4) ---
        if self.rate_limiter is not None:
            try:
                self.rate_limiter.check(api_key)
            except QuotaExceeded as exc:
                self._log(tool_name, api_key, start, False, "rate_limited")
                return self._error("rate_limited", str(exc))

        # --- Cache (Stage 5) ---
        cache_key = None
        if self.cache is not None and tool_name.startswith("ask_model"):
            cache_key = make_cache_key(tool_name, arguments)
            cached = await self.cache.get(cache_key)
            if cached is not None:
                self._log(tool_name, api_key, start, True, None, cache_hit=True)
                return {**cached, "cache_hit": True}

        # --- Execute ---
        ctx = RequestContext(
            api_key=api_key, scopes=scopes, omniroute=self.omniroute,
            cache=self.cache, metrics=self.metrics, request_id=request_id,
        )
        try:
            result = await spec.handler(ctx=ctx, **arguments)
        except TypeError as exc:
            self._log(tool_name, api_key, start, False, "bad_arguments")
            return self._error("bad_arguments", str(exc))
        except Exception:
            logger.exception("nexus.tool_unhandled_exception")
            self._log(tool_name, api_key, start, False, "internal_error")
            return self._error("internal_error", "An internal error occurred.")

        if cache_key is not None and not result.get("error"):
            await self.cache.set(cache_key, result)

        tokens_used, cost_estimate = self._extract_usage(result)
        self._log(
            tool_name, api_key, start, not result.get("error", False),
            result.get("error_type") if result.get("error") else None,
            tokens_used=tokens_used, cost_estimate=cost_estimate,
        )
        return result

    async def dispatch_stream(
        self, tool_name: str, arguments: dict, api_key: str
    ) -> AsyncIterator[dict]:
        """Streaming counterpart of dispatch() (Section 6): for tools whose
        handler is an async generator instead of a coroutine. Runs the same
        auth/scope/rate-limit checks up front (no caching — the spec only
        asks for caching on ask_model-style single-shot calls), then yields
        partial chunks as the handler produces them.

        Not yet exercised end-to-end with a real streaming tool — research_chain
        and code_review (the two tools meant to use this) land in Section 6's
        tool build-out. Until then this is verified via a synthetic streaming
        tool in tests/test_dispatch_stream.py.
        """
        spec = self.registry.get(tool_name)
        if spec is None:
            yield self._error("unknown_tool", f"No such tool: {tool_name}")
            return

        if not spec.streaming:
            yield await self.dispatch(tool_name, arguments, api_key)
            return

        start = time.monotonic()
        scopes, auth_err = await self._check_auth_and_scope(tool_name, spec, api_key)
        if auth_err is not None:
            self._log(tool_name, api_key, start, False, auth_err["error_type"])
            yield auth_err
            return

        if self.rate_limiter is not None:
            try:
                self.rate_limiter.check(api_key)
            except QuotaExceeded as exc:
                self._log(tool_name, api_key, start, False, "rate_limited")
                yield self._error("rate_limited", str(exc))
                return

        ctx = RequestContext(
            api_key=api_key, scopes=scopes, omniroute=self.omniroute,
            cache=self.cache, metrics=self.metrics, request_id=uuid.uuid4().hex[:12],
        )
        total_tokens = 0
        total_cost = 0.0
        try:
            async for chunk in spec.handler(ctx=ctx, **arguments):
                total_tokens += chunk.get("tokens_used", 0)
                total_cost += chunk.get("cost_estimate", 0.0)
                yield chunk
            self._log(tool_name, api_key, start, True, None, tokens_used=total_tokens, cost_estimate=total_cost)
        except Exception:
            logger.exception("nexus.stream_tool_unhandled_exception")
            self._log(tool_name, api_key, start, False, "internal_error", tokens_used=total_tokens, cost_estimate=total_cost)
            yield self._error("internal_error", "An internal error occurred while streaming.")

    def _error(self, error_type: str, message: str) -> dict:
        return {"error": True, "error_type": error_type, "message": message}

    def _extract_usage(self, result: dict) -> tuple[int, float]:
        """Pulls (tokens_used, cost_estimate) out of a tool result for the
        structured log line. Most tools return these as flat top-level
        fields; compare_models_v1 returns a `results` list (one entry per
        model), so its usage is the sum across whichever entries succeeded.
        Unknown shapes just log zero rather than guessing."""
        if "tokens_used" in result:
            return result.get("tokens_used", 0), result.get("cost_estimate", 0.0)
        if isinstance(result.get("results"), list):
            tokens = sum(r.get("tokens_used", 0) for r in result["results"] if not r.get("error"))
            cost = sum(r.get("cost_estimate", 0.0) for r in result["results"] if not r.get("error"))
            return tokens, cost
        return 0, 0.0

    def _log(self, tool_name, api_key, start, success, error_type, cache_hit=False, tokens_used=0, cost_estimate=0.0):
        if self.metrics is not None:
            self.metrics.record(
                tool=tool_name,
                api_key=api_key,
                latency_ms=(time.monotonic() - start) * 1000,
                success=success,
                error_type=error_type,
                cache_hit=cache_hit,
                tokens_used=tokens_used,
                cost_estimate=cost_estimate,
            )
