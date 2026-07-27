"""
ask_model_v1 — ask a single model a prompt via OmniRoute.

This is the simplest tool and the one used to validate the end-to-end path
in Stage 1 (stdio only, no auth/rate-limit/cache yet — those wrap around
this same handler in later stages without changing its signature).
"""
from __future__ import annotations

from context import RequestContext
from omniroute_client import OmniRouteError
from registry import tool

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "The prompt to send to the model."},
        "model": {
            "type": "string",
            "description": "Model id to use. Defaults to the server's configured default model.",
        },
    },
    "required": ["prompt"],
}


@tool(
    name="ask_model_v1",
    description="Ask a single LLM (via OmniRoute) a prompt and get back its response.",
    input_schema=INPUT_SCHEMA,
    scopes=["model:read"],
)
async def ask_model_v1(prompt: str, ctx: RequestContext, model: str | None = None) -> dict:
    try:
        result = await ctx.omniroute.complete(prompt=prompt, model=model)
    except OmniRouteError as exc:
        return {
            "error": True,
            "error_type": exc.error_type,
            "message": str(exc),
        }
    return {
        "error": False,
        "model": result.model,
        "text": result.text,
        "tokens_used": result.tokens_used,
        "cost_estimate": result.cost_estimate,
        "latency_ms": round(result.latency_ms, 1),
    }
