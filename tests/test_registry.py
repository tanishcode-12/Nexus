from __future__ import annotations

import pytest

from registry import ToolRegistry, ToolSpec


async def _dummy_handler(ctx, **kwargs):
    return {"ok": True}


def test_register_and_get():
    reg = ToolRegistry()
    spec = ToolSpec(
        name="dummy_v1", description="d", input_schema={}, scopes=[], handler=_dummy_handler
    )
    reg.register(spec)
    assert reg.get("dummy_v1") is spec
    assert reg.get("nonexistent") is None


def test_duplicate_registration_raises():
    reg = ToolRegistry()
    spec = ToolSpec(
        name="dummy_v1", description="d", input_schema={}, scopes=[], handler=_dummy_handler
    )
    reg.register(spec)
    with pytest.raises(ValueError):
        reg.register(spec)


def test_all_and_clear():
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="a_v1", description="d", input_schema={}, scopes=[], handler=_dummy_handler)
    )
    reg.register(
        ToolSpec(name="b_v1", description="d", input_schema={}, scopes=[], handler=_dummy_handler)
    )
    assert {s.name for s in reg.all()} == {"a_v1", "b_v1"}
    reg.clear()
    assert reg.all() == []


def test_ask_model_discovered_in_global_registry(_ensure_tools_discovered):
    from registry import registry

    spec = registry.get("ask_model_v1")
    assert spec is not None
    assert spec.scopes == ["model:read"]
    assert "prompt" in spec.input_schema["properties"]
