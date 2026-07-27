"""
Simple asyncio-based load test for a running Nexus HTTP instance.

This is NOT a substitute for a proper multi-process/production load test —
see README "What's tested vs. untested". It exercises rate limiting, quota
tracking, and caching behavior under concurrent load against a SINGLE Nexus
process (the Flask dev server, or one gunicorn worker). It does NOT tell you
how rate limiting behaves across multiple gunicorn worker PROCESSES, since
the token bucket is in-process memory — each process would have its own,
independent bucket. See README for why that matters and what to do about it.

Usage:
    python scripts/load_test.py --base-url http://127.0.0.1:8080 \\
        --api-key sk-... --concurrency 20 --requests-per-client 10

    # to specifically demonstrate rate limiting kicking in, point --api-key
    # at a key configured with a small rate_capacity in keys.json

Prints a JSON summary: status code breakdown + latency percentiles.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter

import httpx


async def _one_client(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    n_requests: int,
    results: list[tuple[str | int, float]],
) -> None:
    for i in range(n_requests):
        start = time.monotonic()
        try:
            resp = await client.post(
                f"{base_url}/v1/tools/call",
                headers={"X-API-Key": api_key},
                json={"tool": "ask_model_v1", "arguments": {"prompt": f"load test request {i}"}},
                timeout=10.0,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            results.append((resp.status_code, elapsed_ms))
        except httpx.HTTPError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            results.append((f"transport_error:{type(exc).__name__}", elapsed_ms))


async def run_load_test(
    base_url: str, api_key: str, concurrency: int, requests_per_client: int
) -> dict:
    results: list[tuple[str | int, float]] = []
    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *(
                _one_client(client, base_url, api_key, requests_per_client, results)
                for _ in range(concurrency)
            )
        )
    return _summarize(results)


def _summarize(results: list[tuple[str | int, float]]) -> dict:
    status_counts = Counter(r[0] for r in results)
    latencies = sorted(r[1] for r in results)

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = min(len(latencies) - 1, int(len(latencies) * p))
        return round(latencies[idx], 2)

    return {
        "total_requests": len(results),
        "status_counts": dict(status_counts),
        "latency_ms": {
            "min": round(min(latencies), 2) if latencies else 0,
            "p50": pct(0.50),
            "p95": pct(0.95),
            "max": round(max(latencies), 2) if latencies else 0,
            "mean": round(statistics.mean(latencies), 2) if latencies else 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus asyncio load test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests-per-client", type=int, default=10)
    args = parser.parse_args()

    summary = asyncio.run(
        run_load_test(args.base_url, args.api_key, args.concurrency, args.requests_per_client)
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
