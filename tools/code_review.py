"""
code_review_v1 — review a code snippet for bugs, style, and suggestions.

For large snippets, the input is split into line-based chunks; each chunk
is reviewed and streamed back independently, followed by one final overall
summary chunk — rather than blocking on a single giant review (Section 6).

Known simplification: chunking is a blind line count, not an AST/
tree-sitter-aware split on function or class boundaries. A chunk can cut a
function in half, which costs the model some context. Flagged here rather
than silently shipped as if it were smarter than it is.
"""
from __future__ import annotations

from context import RequestContext
from omniroute_client import OmniRouteError
from registry import tool

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "snippet": {"type": "string", "description": "The code to review."},
        "language": {"type": "string", "description": "Programming language of the snippet."},
    },
    "required": ["snippet", "language"],
}

_CHUNK_LINES = 40
_LARGE_THRESHOLD_LINES = 40


def _chunk_lines(snippet: str, chunk_size: int) -> list[str]:
    lines = snippet.splitlines()
    chunks = ["\n".join(lines[i : i + chunk_size]) for i in range(0, len(lines), chunk_size)]
    return chunks or [snippet]


@tool(
    name="code_review_v1",
    description="Review a code snippet for bugs, style issues, and suggestions. Streams chunk-by-chunk for large inputs.",
    input_schema=INPUT_SCHEMA,
    scopes=["code:review"],
    streaming=True,
)
async def code_review_v1(snippet: str, language: str, ctx: RequestContext):
    line_count = snippet.count("\n") + 1
    chunks = _chunk_lines(snippet, _CHUNK_LINES) if line_count > _LARGE_THRESHOLD_LINES else [snippet]
    total = len(chunks)
    will_have_summary = total > 1  # a summary phase follows iff we actually chunked

    reviews: list[str] = []
    for i, chunk in enumerate(chunks):
        part_note = f" (part {i + 1} of {total} of a larger file)" if total > 1 else ""
        prompt = (
            f"You are reviewing {language} code{part_note}. Point out bugs, style "
            f"issues, and concrete suggestions. Reference line content, not line "
            f"numbers (chunk boundaries may not match the original file's numbering)."
            f"\n\n```{language}\n{chunk}\n```"
        )
        try:
            result = await ctx.omniroute.complete(prompt=prompt)
        except OmniRouteError as exc:
            yield {
                "error": True, "error_type": exc.error_type, "message": str(exc),
                "chunk_index": i, "total_chunks": total,
            }
            return

        reviews.append(result.text)
        yield {
            "error": False,
            "partial": will_have_summary,  # False only when this chunk IS the final result
            "chunk_index": i,
            "total_chunks": total,
            "text": result.text,
            "model": result.model,
            "tokens_used": result.tokens_used,
            "cost_estimate": result.cost_estimate,
        }

    if will_have_summary:
        summary_prompt = (
            f"You reviewed a {language} file in {total} parts. Here are the per-part "
            f"reviews:\n\n" + "\n\n---\n\n".join(reviews) +
            "\n\nWrite a short overall summary: the most important issues across the "
            "whole file, prioritized."
        )
        try:
            summary = await ctx.omniroute.complete(prompt=summary_prompt)
        except OmniRouteError as exc:
            yield {"error": True, "error_type": exc.error_type, "message": str(exc), "phase": "summary"}
            return

        yield {
            "error": False,
            "partial": False,
            "phase": "summary",
            "text": summary.text,
            "model": summary.model,
            "tokens_used": summary.tokens_used,
            "cost_estimate": summary.cost_estimate,
        }
