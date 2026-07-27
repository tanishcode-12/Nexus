"""
list_available_models_v1 — list the model ids currently exposed by
OmniRoute.
"""
from __future__ import annotations

from context import RequestContext
from omniroute_client import OmniRouteError
from registry import tool

INPUT_SCHEMA = {"type": "object", "properties": {}}


@tool(
    name="list_available_models_v1",
    description="List the model ids currently available through OmniRoute.",
    input_schema=INPUT_SCHEMA,
    scopes=["model:read"],
)
async def list_available_models_v1(ctx: RequestContext) -> dict:
    try:
        models = await ctx.omniroute.list_models()
    except OmniRouteError as exc:
        return {"error": True, "error_type": exc.error_type, "message": str(exc)}
    return {"error": False, "models": models, "count": len(models)}
