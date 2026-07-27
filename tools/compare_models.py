"""
compare_models_v1 — ask multiple models the same prompt concurrently and
return each one's response side by side. Partial failure (some models
succeed, some don't) is not treated as a whole-tool failure; only "every
model failed" is.
"""
from __future__ import annotations

import asyncio

from context import RequestContext
from omniroute_client import OmniRouteError
from registry import tool

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "The prompt to send to every model."},
        "models": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Model ids to compare.",
        },
    },
    "required": ["prompt", "models"],
}


@tool(
    name="compare_models_v1",
    description="Ask multiple LLMs (via OmniRoute) the same prompt concurrently and compare responses.",
    input_schema=INPUT_SCHEMA,
    scopes=["model:read"],
)
async def compare_models_v1(prompt: str, models: list[str], ctx: RequestContext) -> dict:
    async def _one(model: str) -> dict:
        try:
            result = await ctx.omniroute.complete(prompt=prompt, model=model)
            return {
                "model": model,
                "error": False,
                "text": result.text,
                "tokens_used": result.tokens_used,
                "cost_estimate": result.cost_estimate,
                "latency_ms": round(result.latency_ms, 1),
            }
        except OmniRouteError as exc:
            return {"model": model, "error": True, "error_type": exc.error_type, "message": str(exc)}

    results = await asyncio.gather(*(_one(m) for m in models))
    all_failed = all(r["error"] for r in results)

    out = {
        "error": all_failed,
        "results": results,
        "succeeded": sum(1 for r in results if not r["error"]),
        "failed": sum(1 for r in results if r["error"]),
    }
    if all_failed:
        out["error_type"] = "upstream_unavailable"
        out["message"] = "All requested models failed."
    return out
