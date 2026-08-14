"""
code_review_v1 — review a code snippet for bugs, style, and suggestions.

For large snippets, the input is split into chunks; each chunk is reviewed
and streamed back independently, followed by one final overall summary
chunk — rather than blocking on a single giant review (Section 6).

Chunking is boundary-aware but still line-count-based, not a real AST/
tree-sitter parse: it targets ~_CHUNK_TARGET_LINES lines per chunk but
looks for a nearby "safe" cut point first — a blank line, or a line with
no leading whitespace (a plausible top-level statement/def/class in most
common code styles) — rather than always slicing at a fixed offset. This
avoids cutting a function or block in half in the common case without
needing a real per-language parser. It's still a heuristic, not a
guarantee: a chunk can never grow past _CHUNK_HARD_CAP_LINES even with no
boundary in sight, so one long function without blank lines just gets a
hard cut rather than an unbounded chunk. Flagged here rather than shipped
as if it were smarter than it is.
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

_CHUNK_TARGET_LINES = 40
# a chunk is never allowed to grow past this, boundary or not
_CHUNK_HARD_CAP_LINES = 60
_LARGE_THRESHOLD_LINES = 40


def _is_boundary(lines: list[str], idx: int) -> bool:
    """True if starting a new chunk at `idx` is a reasonable cut point: the
    previous line is blank, or the line at `idx` itself has no leading
    whitespace (so it's not sitting inside an indented block). Used to
    avoid slicing through the middle of a function/class body."""
    if idx <= 0 or idx >= len(lines):
        return True
    if lines[idx - 1].strip() == "":
        return True
    line = lines[idx]
    return bool(line) and not line[0].isspace()


def _chunk_lines(snippet: str, target_size: int, hard_cap: int) -> list[str]:
    """Split into chunks of around `target_size` lines each, preferring a
    boundary (see `_is_boundary`) over a fixed offset. Searches outward
    from the ideal cut point for the nearest boundary within
    [target_size // 2, hard_cap] lines of the chunk's start; if none
    exists in that range (e.g. one long, deeply nested function with no
    blank lines), forces a cut at `hard_cap` so chunk size stays bounded."""
    lines = snippet.splitlines()
    if not lines:
        return [snippet]

    chunks: list[str] = []
    start = 0
    n = len(lines)
    while start < n:
        remaining = n - start
        if remaining <= target_size:
            end = n
        else:
            ideal = start + target_size
            limit = min(start + hard_cap, n)
            min_end = start + target_size // 2
            end = None
            max_offset = max(ideal - min_end, limit - ideal)
            for offset in range(0, max_offset + 1):
                for cand in (ideal - offset, ideal + offset):
                    if cand < min_end or cand > limit:
                        continue
                    if _is_boundary(lines, cand):
                        end = cand
                        break
                if end is not None:
                    break
            if end is None:
                end = limit  # no boundary found in range: force cut, bounded by hard_cap
        chunks.append("\n".join(lines[start:end]))
        start = end
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
    chunks = (
        _chunk_lines(snippet, _CHUNK_TARGET_LINES, _CHUNK_HARD_CAP_LINES)
        if line_count > _LARGE_THRESHOLD_LINES
        else [snippet]
    )
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
