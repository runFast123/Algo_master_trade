"""Vercel entry point.

Vercel's Python runtime looks for an ASGI app in this file, so this exists only
to expose the one in ``app/main.py``. There is no second implementation.

**The database must not be SQLite here.** A serverless filesystem is read-only
apart from /tmp, and /tmp does not survive between invocations — a SQLite file
would be silently recreated empty, so every licence issued would vanish and
every client would be told their key is unrecognised. Set
``LICENCE_DATABASE_URL`` to a hosted Postgres before deploying.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if (os.environ.get("LICENCE_DATABASE_URL", "").startswith("sqlite")
        or not os.environ.get("LICENCE_DATABASE_URL")):
    raise RuntimeError(
        "LICENCE_DATABASE_URL must point at a hosted database (Postgres) when "
        "running serverless. A SQLite file cannot persist between invocations, "
        "so licences would disappear silently rather than failing loudly."
    )

from app.main import app  # noqa: E402 - path is set above

# Re-exported for the Vercel runtime, which looks for an ASGI app here.
__all__ = ["app"]
