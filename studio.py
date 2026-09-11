#!/usr/bin/env python3
"""Skyground Video Studio.

The command line entry point. The editorial commands need nothing but Python:

    python3 studio.py list
    python3 studio.py status beauty-centers-growth-01
    python3 studio.py validate beauty-centers-growth-01
    python3 studio.py sync beauty-centers-growth-01
    python3 studio.py build-source beauty-centers-growth-01
    python3 studio.py render beauty-centers-growth-01
    python3 studio.py serve

The implementation lives in the `skyground` package: `skyground.core` for the
video project itself, `skyground.api` for the web application, `skyground.worker`
for the render queue.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from skyground.cli import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
