"""
research_chain_v1 — chains two OmniRoute calls (research, then synthesize)
and streams the research-phase result back as soon as it's ready, instead
of blocking until both calls finish (Section 6).
"""
from __future__ import annotations

from context import RequestContext
from omniroute_client import OmniRouteError
from registry import tool

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "The topic to research and synthesize."},
    },
    "required": ["topic"],
}


@tool(
    name="research_chain_v1",
    description=(
        "Research a topic and synthesize a concise answer via two chained OmniRoute "
        "calls. Streams the research phase back before synthesis completes."
    ),
    input_schema=INPUT_SCHEMA,
    scopes=["model:read"],
    streaming=True,
)
async def research_chain_v1(topic: str, ctx: RequestContext):
    try:
        research = await ctx.omniroute.complete(
            prompt=(
                f"Research the topic '{topic}'. List the key facts, context, and any "
                f"important nuance or disagreement. Be concise but thorough."
            )
        )
    except OmniRouteError as exc:
        yield {"error": True, "error_type": exc.error_type, "message": str(exc), "phase": "research"}
        return

    yield {
        "error": False,
        "phase": "research",
        "partial": True,
        "text": research.text,
        "model": research.model,
        "tokens_used": research.tokens_used,
        "cost_estimate": research.cost_estimate,
    }

    try:
        synthesis = await ctx.omniroute.complete(
            prompt=(
                f"Based on this research about '{topic}':\n\n{research.text}\n\n"
                f"Write a clear, well-organized synthesis for someone learning about "
                f"this topic for the first time."
            )
        )
    except OmniRouteError as exc:
        yield {"error": True, "error_type": exc.error_type, "message": str(exc), "phase": "synthesis"}
        return

    yield {
        "error": False,
        "phase": "synthesis",
        "partial": False,
        "text": synthesis.text,
        "model": synthesis.model,
        "tokens_used": synthesis.tokens_used,
        "cost_estimate": synthesis.cost_estimate,
    }
