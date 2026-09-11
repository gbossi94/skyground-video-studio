"""ASGI entry point: `uvicorn skyground.asgi:app`."""

from skyground.api.app import build_default_app

app = build_default_app()
