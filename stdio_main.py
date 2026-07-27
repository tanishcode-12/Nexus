"""
Thin stdio transport adapter — for Claude Desktop and other local MCP
clients. Contains NO tool logic; it just wires the shared NexusServer's
mcp.server.lowlevel.Server onto mcp.server.stdio's stdio_server().

Auth for stdio comes from the NEXUS_API_KEY environment variable (set by
whoever configures the Claude Desktop MCP server entry) rather than an HTTP
header, since there's no per-request header concept over stdio.
"""
from __future__ import annotations

import asyncio
import logging

import mcp.server.stdio

from auth import AuthStore
from cache import build_cache
from config import config
from core import NexusServer
from metrics import MetricsRecorder, configure_json_logging
from omniroute_client import OmniRouteClient
from ratelimit import RateLimiter

logger = logging.getLogger("nexus.stdio")


async def run() -> None:
    configure_json_logging(config.log_level)

    omniroute = OmniRouteClient(
        base_url=config.omniroute_base_url,
        api_key=config.omniroute_api_key,
        default_model=config.default_model,
        fallback_model=config.fallback_model,
    )
    auth_store = AuthStore(keys_file=config.keys_file)
    rate_limiter = RateLimiter(config, auth_store)
    cache = build_cache(config.cache_backend, config.redis_url, config.cache_ttl_seconds)
    metrics = MetricsRecorder()

    server = NexusServer(
        omniroute=omniroute,
        auth_store=auth_store,
        rate_limiter=rate_limiter,
        cache=cache,
        metrics=metrics,
    )

    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.mcp_server.run(
                read_stream,
                write_stream,
                server.mcp_server.create_initialization_options(),
            )
    finally:
        await omniroute.aclose()


if __name__ == "__main__":
    asyncio.run(run())
