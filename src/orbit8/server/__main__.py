"""Run the API: `python -m orbit8.server`."""
from __future__ import annotations

import os

import uvicorn

from .app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("ORBIT8_HOST", "127.0.0.1"),
                port=int(os.environ.get("ORBIT8_PORT", "8000")))
