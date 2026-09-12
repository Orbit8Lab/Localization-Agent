"""ASGI entry point for a production server: `orbit8.server.asgi:app`.

Separate from `__main__` because a module named `__main__` is re-executed
when imported under that name, which is a confusing failure when a
process manager imports it. This module only builds the app.
"""
from __future__ import annotations

from .app import create_app

app = create_app()
