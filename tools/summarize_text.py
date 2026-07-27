"""
summarize_text_v1 — summarize `text` in at most `max_words` words.

Models don't reliably respect an exact word budget from prompting alone, so
this enforces max_words as a hard truncation safety net on top of the
prompt instruction, and reports whether truncation actually kicked in.
"""
from __future__ import annotations

from context import RequestContext
from omniroute_client import OmniRouteError
from registry import tool

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "The text to summarize."},
        "max_words": {
            "type": "integer",
            "minimum": 5,
            "description": "Target maximum word count for the summary.",
        },
    },
    "required": ["text", "max_words"],
}


@tool(
    name="summarize_text_v1",
    description="Summarize a block of text in at most max_words words.",
    input_schema=INPUT_SCHEMA,
    scopes=["model:read"],
)
async def summarize_text_v1(text: str, max_words: int, ctx: RequestContext) -> dict:
    prompt = (
        f"Summarize the following text in {max_words} words or fewer. "
        f"Output only the summary, no preamble or headers.\n\n---\n{text}\n---"
    )
    try:
        result = await ctx.omniroute.complete(prompt=prompt)
    except OmniRouteError as exc:
        return {"error": True, "error_type": exc.error_type, "message": str(exc)}

    summary = result.text
    words = summary.split()
    truncated = False
    if len(words) > max_words:
        summary = " ".join(words[:max_words])
        truncated = True

    return {
        "error": False,
        "summary": summary,
        "word_count": len(summary.split()),
        "truncated_by_nexus": truncated,
        "model": result.model,
        "tokens_used": result.tokens_used,
        "cost_estimate": result.cost_estimate,
    }
