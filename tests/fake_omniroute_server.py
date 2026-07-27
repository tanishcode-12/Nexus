"""
A tiny real HTTP server that speaks OmniRoute's wire format, used so tests
can exercise OmniRouteClient (and everything above it) over an actual
socket — not just a mocked transport. This is the closest thing to "real
OmniRoute" reachable from this sandbox, which has no network egress to a
localhost/LAN gateway on a real machine. It is NOT a substitute for testing
against your actual OmniRoute instance — see README.

Behavior:
  GET  /v1/models                -> a small fixed model list
  POST /v1/chat/completions      -> echoes the prompt back in a canned
                                     completion, UNLESS the requested model
                                     is "always-fails" (used to test retry
                                     + fallback) or "flaky" (fails on the
                                     first N calls, then succeeds — used to
                                     test retry-then-succeed).
"""
from __future__ import annotations

import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

_flaky_call_counts: dict[str, int] = {}


def make_fake_omniroute_app() -> Flask:
    app = Flask("fake_omniroute")
    _flaky_call_counts.clear()

    @app.get("/v1/models")
    def list_models():
        return jsonify({"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}, {"id": "claude-sonnet-5"}]})

    @app.post("/v1/chat/completions")
    def chat_completions():
        body = request.get_json(force=True)
        model = body.get("model", "")
        prompt = body["messages"][0]["content"]

        if model == "always-fails":
            return jsonify({"error": "simulated upstream failure"}), 503

        if model == "flaky":
            _flaky_call_counts["flaky"] = _flaky_call_counts.get("flaky", 0) + 1
            if _flaky_call_counts["flaky"] <= 2:
                return jsonify({"error": "simulated transient failure"}), 503
            # third call succeeds

        return jsonify(
            {
                "choices": [{"message": {"content": f"[fake:{model}] response to: {prompt}"}}],
                "usage": {"total_tokens": max(10, len(prompt.split()) * 2)},
            }
        )

    return app


class FakeOmniRouteServer:
    """Runs make_fake_omniroute_app() on a background thread bound to
    localhost on an OS-assigned free port."""

    def __init__(self):
        self.app = make_fake_omniroute_app()
        self._server = make_server("127.0.0.1", 0, self.app)
        self.port = self._server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> FakeOmniRouteServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
