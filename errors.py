"""
Maps Nexus's transport-agnostic error_type strings (produced by core.dispatch)
to HTTP status codes for the Flask adapter. Kept out of core.py on purpose —
core.py's dispatch() is shared by stdio too, and stdio has no concept of an
HTTP status code.
"""
from __future__ import annotations

HTTP_STATUS_BY_ERROR_TYPE = {
    "unknown_tool": 404,
    "auth_error": 401,
    "forbidden_scope": 403,
    "rate_limited": 429,
    "bad_arguments": 400,
    "upstream_unavailable": 502,
    "internal_error": 500,
}


def http_status_for(result: dict) -> int:
    if not result.get("error"):
        return 200
    return HTTP_STATUS_BY_ERROR_TYPE.get(result.get("error_type"), 500)
