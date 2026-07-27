"""
Stage 1 end-to-end proof: spawns stdio_main.py as a REAL subprocess, talks
to it over REAL stdio using an MCP ClientSession (not calling Python
functions directly in-process), and the subprocess makes a REAL HTTP call
out to the fake OmniRoute server on an OS-assigned localhost port.

This is the strongest verification available in this sandbox short of your
actual Claude Desktop + your actual OmniRoute instance. It proves: process
startup, tool auto-discovery, the MCP stdio wire protocol (initialize ->
list_tools -> call_tool), env-var-based auth, and the OmniRoute HTTP client
— all for real, not mocked.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_stdio_end_to_end_ask_model(fake_omniroute, tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"api_key": "sk-e2e-test-key", "scopes": ["model:read"]}]})
    )
    db_path = tmp_path / "e2e_quota.sqlite3"

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "OMNIROUTE_BASE_URL": fake_omniroute.base_url,
        "OMNIROUTE_API_KEY": "irrelevant-for-fake-server",
        "NEXUS_API_KEY": "sk-e2e-test-key",
        "NEXUS_KEYS_FILE": str(keys_file),
        "NEXUS_DB_PATH": str(db_path),
        "NEXUS_CACHE_BACKEND": "memory",
        "PYTHONPATH": project_root,
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(project_root, "stdio_main.py")],
        env=env,
        cwd=project_root,
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools_result = await session.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        assert "ask_model_v1" in tool_names

        call_result = await session.call_tool(
            "ask_model_v1", arguments={"prompt": "what is the capital of France?"}
        )
        payload = json.loads(call_result.content[0].text)
        assert payload["error"] is False
        assert "capital of France" in payload["text"]
        assert payload["model"] == "gpt-4o-mini"  # NEXUS_DEFAULT_MODEL default


@pytest.mark.asyncio
async def test_stdio_end_to_end_rejects_bad_scope(fake_omniroute, tmp_path):
    """Same real subprocess path, but the env-based key only has a scope
    that doesn't cover ask_model_v1 — proves auth/scoping is really wired
    through the stdio transport, not just the HTTP one."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"api_key": "sk-readonly-nothing", "scopes": ["nothing:useful"]}]})
    )
    db_path = tmp_path / "e2e_quota2.sqlite3"
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "OMNIROUTE_BASE_URL": fake_omniroute.base_url,
        "OMNIROUTE_API_KEY": "irrelevant",
        "NEXUS_API_KEY": "sk-readonly-nothing",
        "NEXUS_KEYS_FILE": str(keys_file),
        "NEXUS_DB_PATH": str(db_path),
        "PYTHONPATH": project_root,
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(project_root, "stdio_main.py")],
        env=env,
        cwd=project_root,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        call_result = await session.call_tool("ask_model_v1", arguments={"prompt": "hi"})
        payload = json.loads(call_result.content[0].text)
        assert payload["error"] is True
        assert payload["error_type"] == "forbidden_scope"
