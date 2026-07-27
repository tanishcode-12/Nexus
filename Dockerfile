# Nexus — Flask HTTP+SSE transport image.
#
# NOTE: this image runs the HTTP transport only (http_main.py under
# gunicorn). stdio_main.py is meant to run as a local subprocess launched by
# an MCP client (Claude Desktop) directly on the user's machine — it doesn't
# make sense inside a long-running container, so it isn't part of this image.
# See README.

FROM python:3.12-slim AS base

WORKDIR /app

# System deps: none beyond what python:slim already has — gunicorn, Flask,
# httpx, redis-py and mcp are all pure-Python-installable via pip.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user — no reason this process needs root.
RUN useradd --create-home --uid 1000 nexus \
    && mkdir -p /app/data \
    && chown -R nexus:nexus /app
USER nexus

ENV NEXUS_HTTP_HOST=0.0.0.0 \
    NEXUS_HTTP_PORT=8080 \
    NEXUS_DB_PATH=/app/data/nexus_quota.sqlite3 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

# gunicorn, not `flask run` or `app.run()`: this project deliberately stays on
# Flask (WSGI) rather than an ASGI framework (see README "Design Decisions &
# Tradeoffs"), which means SSE + concurrency need a threaded worker class —
# gthread, not the default sync worker (which would serialize every request,
# including in-flight SSE streams, behind one connection at a time).
#
# --workers: each worker is a SEPARATE PROCESS with its own in-memory rate
# limiter state (see README) — 1 here for that exact reason, so rate limits
# are enforced correctly out of the box. Scaling beyond 1 worker/process
# requires moving the token bucket to Redis first; don't just bump this
# number without doing that (flagged loudly in README too).
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", \
     "--worker-class", "gthread", "--timeout", "120", "wsgi:app"]
