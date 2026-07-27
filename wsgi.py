"""
WSGI entrypoint for gunicorn: `gunicorn wsgi:app`.

A tiny separate module rather than pointing gunicorn at some clever
expression on http_main directly — gunicorn's app-spec parser only accepts
a plain attribute name or a function call with literal arguments (verified
by reading gunicorn.util.import_app rather than assumed), so
`http_main:_build_default_server()[0]` would not have worked. This module
just does the tuple-unpacking gunicorn can't express in its CLI syntax.
"""
from __future__ import annotations

from http_main import _build_default_server

app, _server = _build_default_server()
