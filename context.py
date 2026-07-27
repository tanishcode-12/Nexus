"""
RequestContext: the one object every tool handler receives.

It's built up in layers as the server pipeline runs (auth -> rate limit ->
cache -> execute -> log), so each middleware attaches what it knows and the
tool handler just reads off of it. Kept in its own module so tests can
construct a minimal fake context without booting the whole server.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omniroute_client import OmniRouteClient


@dataclass
class RequestContext:
    api_key: str
    scopes: list[str]
    omniroute: OmniRouteClient
    cache: Any = None          # CacheBackend, attached in Stage 5
    metrics: Any = None        # MetricsRecorder, attached in Stage 7
    request_id: str = ""
    extra: dict = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes
