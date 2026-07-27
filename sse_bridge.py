"""
Bridges an async generator (NexusServer.dispatch_stream's output) into a
plain synchronous iterator of SSE-formatted strings, for use inside
Flask's `Response(generator, mimetype="text/event-stream")` pattern.

Why this exists: Flask/WSGI is synchronous, but Nexus's tool handlers are
async (they await OmniRoute HTTP calls). We can't just call the async
generator from a sync Flask view. Instead this runs it to completion on a
background thread with its own event loop, relaying each yielded chunk
across a thread-safe queue.Queue to the synchronous side, which is what
Flask actually streams to the client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import AsyncIterator, Iterator

logger = logging.getLogger("nexus.sse_bridge")


def bridge_to_sse_lines(async_gen: AsyncIterator[dict]) -> Iterator[str]:
    """Consume `async_gen` on a background thread, yield SSE `data: ...`
    lines (plus a terminal `event: done` or `event: error`) on this thread.
    """
    q: queue.Queue[tuple[str, object]] = queue.Queue()

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def consume() -> None:
            try:
                async for chunk in async_gen:
                    q.put(("chunk", chunk))
            except Exception as exc:
                logger.exception("nexus.stream_bridge_unhandled_exception")
                q.put(("error", str(exc)))
            finally:
                q.put(("done", None))

        try:
            loop.run_until_complete(consume())
        finally:
            loop.close()

    threading.Thread(target=runner, daemon=True).start()

    while True:
        kind, payload = q.get()
        if kind == "chunk":
            yield f"data: {json.dumps(payload)}\n\n"
        elif kind == "error":
            yield f"event: error\ndata: {json.dumps({'message': payload})}\n\n"
            return
        else:
            yield "event: done\ndata: {}\n\n"
            return
