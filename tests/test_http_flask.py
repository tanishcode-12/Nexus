from __future__ import annotations

import pytest

from auth import AuthStore, KeyRecord
from cache import InMemoryCache
from config import Config
from core import NexusServer
from http_main import build_app
from metrics import MetricsRecorder
from omniroute_client import OmniRouteClient
from ratelimit import RateLimiter
from registry import ToolRegistry, ToolSpec
from registry import registry as global_registry


async def _synthetic_stream_handler(ctx, topic: str):
    yield {"partial": True, "text": f"researching {topic}"}
    yield {"partial": False, "text": f"done with {topic}"}


def _build_test_app(fake_omniroute, tmp_path, extra_scopes=None, tight_rate_limit=False):
    reg = ToolRegistry()
    reg.register(global_registry.get("ask_model_v1"))
    reg.register(
        ToolSpec(
            name="research_chain_v1",
            description="synthetic streaming tool for SSE tests",
            input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
            scopes=["model:read"],
            handler=_synthetic_stream_handler,
            streaming=True,
        )
    )

    auth_store = AuthStore(
        records=[
            KeyRecord(api_key="key-admin", scopes=["admin"]),
            KeyRecord(api_key="key-readonly", scopes=["model:read"]),
            KeyRecord(
                api_key="key-tight",
                scopes=["model:read"],
                rate_capacity=1 if tight_rate_limit else 100,
                rate_refill_per_sec=0.0 if tight_rate_limit else 1000.0,
                daily_quota=1000,
                monthly_quota=1000,
            ),
        ]
    )
    cfg = Config()
    cfg.db_path = str(tmp_path / "http_test_quota.sqlite3")
    cfg.default_rate_capacity = 100
    cfg.default_rate_refill_per_sec = 1000.0

    omniroute = OmniRouteClient(base_url=fake_omniroute.base_url, api_key="k", max_retries=1, backoff_base_seconds=0.01)
    server = NexusServer(
        omniroute=omniroute,
        auth_store=auth_store,
        rate_limiter=RateLimiter(cfg, auth_store),
        cache=InMemoryCache(),
        metrics=MetricsRecorder(),
        tool_registry=reg,
        discover=False,
    )
    app = build_app(server, auth_store)
    app.testing = True
    return app


@pytest.fixture
def client(fake_omniroute, tmp_path):
    app = _build_test_app(fake_omniroute, tmp_path)
    return app.test_client()


def test_healthz_needs_no_auth(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_missing_api_key_is_401(client):
    resp = client.get("/v1/tools")
    assert resp.status_code == 401
    assert resp.get_json()["error_type"] == "auth_error"


def test_unknown_api_key_is_401(client):
    resp = client.get("/v1/tools", headers={"X-API-Key": "sk-bogus"})
    assert resp.status_code == 401


def test_list_tools_with_valid_key(client):
    resp = client.get("/v1/tools", headers={"X-API-Key": "key-admin"})
    assert resp.status_code == 200
    names = [t["name"] for t in resp.get_json()["tools"]]
    assert "ask_model_v1" in names
    assert "research_chain_v1" in names


def test_call_tool_happy_path(client):
    resp = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-admin"},
        json={"tool": "ask_model_v1", "arguments": {"prompt": "hello flask"}},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["error"] is False
    assert "hello flask" in body["text"]


def test_call_unknown_tool_is_404(client):
    resp = client.post(
        "/v1/tools/call", headers={"X-API-Key": "key-admin"}, json={"tool": "not_a_tool_v1"}
    )
    assert resp.status_code == 404
    assert resp.get_json()["error_type"] == "unknown_tool"


def test_call_tool_missing_tool_field_is_400(client):
    resp = client.post("/v1/tools/call", headers={"X-API-Key": "key-admin"}, json={})
    assert resp.status_code == 400
    assert resp.get_json()["error_type"] == "bad_arguments"


def test_readonly_key_can_call_a_model_read_tool(client):
    resp = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-readonly"},
        json={"tool": "ask_model_v1", "arguments": {"prompt": "hi"}},
    )
    # key-readonly HAS model:read, so this should succeed — sanity check that
    # the auth gate isn't accidentally over-restrictive
    assert resp.status_code == 200


def test_call_restricted_tool_without_scope_is_403(fake_omniroute, tmp_path):
    async def _stub(ctx, **kw):
        return {"error": False, "text": "should never get here"}

    reg = ToolRegistry()
    reg.register(global_registry.get("ask_model_v1"))
    reg.register(
        ToolSpec(
            name="code_review_v1",
            description="restricted",
            input_schema={"type": "object", "properties": {}},
            scopes=["code:review"],
            handler=_stub,
        )
    )
    auth_store = AuthStore(records=[KeyRecord(api_key="key-readonly", scopes=["model:read"])])
    cfg = Config()
    cfg.db_path = str(tmp_path / "scope_test_quota.sqlite3")
    server = NexusServer(
        omniroute=OmniRouteClient(base_url=fake_omniroute.base_url, api_key="k"),
        auth_store=auth_store,
        rate_limiter=RateLimiter(cfg, auth_store),
        cache=InMemoryCache(),
        metrics=MetricsRecorder(),
        tool_registry=reg,
        discover=False,
    )
    client = build_app(server, auth_store).test_client()

    resp = client.post(
        "/v1/tools/call", headers={"X-API-Key": "key-readonly"}, json={"tool": "code_review_v1"}
    )
    assert resp.status_code == 403
    assert resp.get_json()["error_type"] == "forbidden_scope"


def test_unmatched_route_is_clean_404_not_html(client):
    resp = client.get("/this/route/does/not/exist")
    assert resp.status_code == 404
    # proves the blanket errorhandler passes HTTPExceptions through with
    # their real status instead of turning every 404 into a 500
    assert resp.content_type.startswith("application/json")


def test_rate_limited_returns_429(fake_omniroute, tmp_path):
    app = _build_test_app(fake_omniroute, tmp_path, tight_rate_limit=True)
    client = app.test_client()
    r1 = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-tight"},
        json={"tool": "ask_model_v1", "arguments": {"prompt": "one"}},
    )
    r2 = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-tight"},
        json={"tool": "ask_model_v1", "arguments": {"prompt": "two"}},
    )
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r2.get_json()["error_type"] == "rate_limited"


def test_metrics_endpoint_reflects_calls(client):
    client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-admin"},
        json={"tool": "ask_model_v1", "arguments": {"prompt": "for metrics"}},
    )
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "nexus_requests_total" in text
    assert 'tool="ask_model_v1"' in text


def test_sse_streaming_via_real_flask_response(client):
    resp = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-admin", "Accept": "text/event-stream"},
        json={"tool": "research_chain_v1", "arguments": {"topic": "black holes"}},
    )
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/event-stream")
    body = resp.get_data(as_text=True)
    assert 'data: {"partial": true, "text": "researching black holes"}' in body
    assert 'data: {"partial": false, "text": "done with black holes"}' in body
    assert body.strip().endswith("event: done\ndata: {}")


def test_non_streaming_call_ignores_sse_accept_header_if_tool_not_streaming(client):
    # ask_model_v1 isn't a streaming tool, so even with Accept: text/event-stream
    # it should just return normal JSON, not try to stream.
    resp = client.post(
        "/v1/tools/call",
        headers={"X-API-Key": "key-admin", "Accept": "text/event-stream"},
        json={"tool": "ask_model_v1", "arguments": {"prompt": "no streaming here"}},
    )
    assert resp.content_type.startswith("application/json")
