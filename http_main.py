"""
Flask HTTP+SSE transport adapter.

Deliberate departure from the MCP SDK's own HTTP transports: the SDK's
built-in `mcp.server.sse` / `mcp.server.streamable_http` are ASGI (Starlette
+ uvicorn) and don't mount inside Flask's WSGI model. Per this project's
explicit requirement to keep the HTTP layer on Flask (consistent with the
rest of the stack) rather than pull in an ASGI framework, this module is a
hand-rolled HTTP surface — same auth/scope/rate-limit/cache/dispatch
pipeline, just not a literal implementation of the official MCP HTTP
transport wire protocol. See README "Design Decisions & Tradeoffs" for the
full reasoning. If you need a spec-compliant MCP-over-HTTP client to talk
to Nexus (as opposed to a custom client hitting these routes directly),
that's the gap this leaves — flagged here rather than glossed over.

This file is a thin adapter: every route parses the request, calls into
NexusServer.dispatch()/dispatch_stream() (identical to what stdio_main.py
calls), and translates the result into an HTTP response. No tool logic,
auth logic, or rate-limit logic lives here.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import wraps

from flask import Flask, Response, g, jsonify, request, stream_with_context
from werkzeug.exceptions import HTTPException

from auth import AuthStore
from cache import build_cache
from config import config
from core import NexusServer
from errors import http_status_for
from metrics import MetricsRecorder, configure_json_logging
from omniroute_client import OmniRouteClient
from ratelimit import RateLimiter
from sse_bridge import bridge_to_sse_lines

logger = logging.getLogger("nexus.http")


def build_app(nexus_server: NexusServer, auth_store: AuthStore) -> Flask:
    """Factory so tests can build an app around an already-constructed
    NexusServer (with a fake OmniRoute client, temp SQLite db, etc.)
    instead of the module-level singleton wired in __main__ below."""
    app = Flask(__name__)

    def require_api_key(view: Callable) -> Callable:
        """Section 3: central auth gate. Checks header presence and that the
        key is one AuthStore knows about; the finer per-tool scope check
        happens once, centrally, inside NexusServer.dispatch — never
        duplicated per-route or per-tool."""

        @wraps(view)
        def wrapper(*args, **kwargs):
            api_key = request.headers.get("X-API-Key", "")
            if not api_key:
                return jsonify(
                    {"error": True, "error_type": "auth_error", "message": "Missing X-API-Key header."}
                ), 401
            if auth_store.record_for(api_key) is None:
                return jsonify(
                    {"error": True, "error_type": "auth_error", "message": "Unknown API key."}
                ), 401
            g.api_key = api_key
            return view(*args, **kwargs)

        return wrapper

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.errorhandler(Exception)
    def _handle_uncaught(exc: Exception):
        # Section 8 applies at the transport layer too: whatever goes wrong
        # (bad JSON, a bug in a route), the client gets clean structured
        # JSON, never Flask's default HTML traceback page. Routing-level
        # HTTPExceptions (404 for an unmatched path, 405, etc.) are normal
        # and get passed through with their real status code rather than
        # being misreported as a 500.
        if isinstance(exc, HTTPException):
            return jsonify(
                {"error": True, "error_type": "http_error", "message": exc.description}
            ), exc.code
        logger.exception("nexus.http_unhandled_exception")
        return jsonify(
            {"error": True, "error_type": "internal_error", "message": "An internal error occurred."}
        ), 500

    @app.get("/v1/tools")
    @require_api_key
    def list_tools():
        return jsonify(
            {
                "tools": [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "input_schema": spec.input_schema,
                        "scopes": spec.scopes,
                        "streaming": spec.streaming,
                    }
                    for spec in nexus_server.registry.all()
                    if not spec.deprecated
                ]
            }
        )

    @app.post("/v1/tools/call")
    @require_api_key
    def call_tool():
        body = request.get_json(silent=True) or {}
        tool_name = body.get("tool")
        arguments = body.get("arguments", {}) or {}

        if not tool_name:
            return jsonify(
                {"error": True, "error_type": "bad_arguments", "message": "Request body must include a 'tool' field."}
            ), 400

        spec = nexus_server.registry.get(tool_name)
        wants_stream = "text/event-stream" in request.headers.get("Accept", "")

        if spec is not None and spec.streaming and wants_stream:
            async_gen = nexus_server.dispatch_stream(tool_name, arguments, g.api_key)
            return Response(
                stream_with_context(bridge_to_sse_lines(async_gen)),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        result = asyncio.run(nexus_server.dispatch(tool_name, arguments, g.api_key))
        return jsonify(result), http_status_for(result)

    @app.get("/metrics")
    def metrics_endpoint():
        return Response(nexus_server.metrics.render_prometheus(), mimetype="text/plain")

    return app


def _build_default_server() -> tuple[Flask, NexusServer]:
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
    return build_app(server, auth_store), server


if __name__ == "__main__":
    flask_app, _server = _build_default_server()
    # threaded=True: needed so one long-running SSE connection doesn't block
    # every other request on Flask's dev server. For real deployment, see
    # README — gunicorn with a threaded worker class, not `flask run`.
    flask_app.run(host=config.http_host, port=config.http_port, threaded=True)
