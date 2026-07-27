"""
Observability (Section 7).

- Structured JSON log line per request (emitted via the standard `logging`
  module with a JSON formatter, so it plays nicely with any log shipper).
- In-process counters aggregated into a Prometheus-text /metrics endpoint.
  Kept as a plain class (no server dependency) so it's unit-testable on its
  own, per the build order.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger("nexus.requests")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "tool", "api_key_hash", "latency_ms", "tokens_used",
            "cost_estimate", "success", "error_type", "cache_hit",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


def configure_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("nexus")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def hash_api_key(api_key: str) -> str:
    if not api_key:
        return "none"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


@dataclass
class _ToolStats:
    request_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    cache_hits: int = 0


class MetricsRecorder:
    """In-process counters. Swap for a real Prometheus client library if you
    need multi-process aggregation — see README tradeoffs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, _ToolStats] = defaultdict(_ToolStats)

    def record(
        self,
        tool: str,
        api_key: str,
        latency_ms: float,
        success: bool,
        error_type: str | None = None,
        tokens_used: int = 0,
        cost_estimate: float = 0.0,
        cache_hit: bool = False,
    ) -> None:
        with self._lock:
            s = self._stats[tool]
            s.request_count += 1
            s.total_latency_ms += latency_ms
            if not success:
                s.error_count += 1
            if cache_hit:
                s.cache_hits += 1

        logger.info(
            "request_completed",
            extra={
                "tool": tool,
                "api_key_hash": hash_api_key(api_key),
                "latency_ms": round(latency_ms, 2),
                "tokens_used": tokens_used,
                "cost_estimate": cost_estimate,
                "success": success,
                "error_type": error_type,
                "cache_hit": cache_hit,
            },
        )

    def render_prometheus(self) -> str:
        lines = [
            "# HELP nexus_requests_total Total requests per tool",
            "# TYPE nexus_requests_total counter",
        ]
        with self._lock:
            for tool, s in self._stats.items():
                lines.append(f'nexus_requests_total{{tool="{tool}"}} {s.request_count}')
            lines.append("# HELP nexus_errors_total Total failed requests per tool")
            lines.append("# TYPE nexus_errors_total counter")
            for tool, s in self._stats.items():
                lines.append(f'nexus_errors_total{{tool="{tool}"}} {s.error_count}')
            lines.append("# HELP nexus_request_latency_ms_avg Average request latency per tool")
            lines.append("# TYPE nexus_request_latency_ms_avg gauge")
            for tool, s in self._stats.items():
                avg = s.total_latency_ms / s.request_count if s.request_count else 0.0
                lines.append(f'nexus_request_latency_ms_avg{{tool="{tool}"}} {avg:.2f}')
            lines.append("# HELP nexus_cache_hits_total Cache hits per tool")
            lines.append("# TYPE nexus_cache_hits_total counter")
            for tool, s in self._stats.items():
                lines.append(f'nexus_cache_hits_total{{tool="{tool}"}} {s.cache_hits}')
        return "\n".join(lines) + "\n"
