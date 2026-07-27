"""
Plugin registry for Nexus tools.

Tools self-register by decorating a handler function with @tool(...).
The server discovers and loads them by importing every module under tools/
at startup — adding a tool means dropping a new file in tools/, not editing
a central list here.
"""
from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[..., Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str                      # versioned name, e.g. "ask_model_v1"
    description: str
    input_schema: dict
    scopes: list[str]              # scopes required to call this tool
    handler: Handler
    streaming: bool = False        # True if handler is an async generator
    version: int = 1
    deprecated: bool = False


class ToolRegistry:
    """Global registry populated by the @tool decorator at import time."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool registration: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def clear(self) -> None:
        """Used by tests to reset global state between test modules."""
        self._tools.clear()


# Single process-wide registry instance.
registry = ToolRegistry()


def tool(
    name: str,
    description: str,
    input_schema: dict,
    scopes: list[str],
    streaming: bool = False,
    version: int = 1,
    deprecated: bool = False,
):
    """Decorator that registers an async function as an MCP tool.

    Usage:
        @tool(
            name="ask_model_v1",
            description="Ask a single model a prompt via OmniRoute",
            input_schema={...},
            scopes=["model:read"],
        )
        async def ask_model_v1(prompt: str, model: str, ctx) -> dict:
            ...
    """

    def decorator(fn: Handler) -> Handler:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_schema=input_schema,
                scopes=scopes,
                handler=fn,
                streaming=streaming,
                version=version,
                deprecated=deprecated,
            )
        )
        return fn

    return decorator


def discover_tools(package_name: str = "tools") -> int:
    """Import every module under `tools/` so their @tool decorators run.

    Returns the number of modules imported. Safe to call more than once
    (re-importing is a no-op for already-loaded modules), but tests that
    need a clean slate should call registry.clear() first.
    """
    package = importlib.import_module(package_name)
    count = 0
    for _finder, mod_name, _is_pkg in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package_name}.{mod_name}")
        count += 1
    return count
