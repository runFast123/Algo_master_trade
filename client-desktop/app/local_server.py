"""Local server: serves the UI and proxies /api/* to the backend.

There is one copy of the UI, in ``frontend-user/``. A development run serves it
directly from there; a frozen build serves the copy PyInstaller bundled at
build time. Nothing is maintained in two places.
"""

import logging
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import desktop_config
from .proxy import proxy_to_backend

logger = logging.getLogger("local_server")

app = FastAPI(title=desktop_config.APP_NAME, docs_url=None, redoc_url=None)


def resolve_ui_dir() -> str:
    """Where the UI is served from, in build and in development."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "static")

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    source_ui = os.path.join(repo_root, "frontend-user")
    if os.path.exists(os.path.join(source_ui, "index.html")):
        return source_ui
    return os.path.abspath(os.path.join(here, "..", "static"))


UI_DIR = resolve_ui_dir()
INDEX_FILE = os.path.join(UI_DIR, "index.html")


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def api_proxy(request: Request, path: str):
    return await proxy_to_backend(request, path)


if os.path.isdir(UI_DIR):
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")


@app.get("/admin")
async def serve_admin():
    """The admin dashboard, served from the app so its /api calls are same-origin.

    Opening the file directly cannot work: the page calls /api/v1/... relatively,
    which only resolves when the API is on the same origin.
    """
    for candidate in (
        os.path.join(UI_DIR, "admin.html"),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                     "frontend-admin", "index.html")),
    ):
        if os.path.exists(candidate):
            return FileResponse(candidate)
    return HTMLResponse(status_code=404, content="Admin dashboard not bundled.")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE)
    return HTMLResponse(
        status_code=500,
        content=f"""
        <html><head><title>{desktop_config.APP_NAME}</title></head>
        <body style="font-family: sans-serif; background:#0f172a; color:#f8fafc;
                     padding:3rem; text-align:center;">
            <h1 style="color:#38bdf8;">{desktop_config.APP_NAME}</h1>
            <p>The user interface was not found at <code>{UI_DIR}</code>.</p>
            <p>Rebuild the application, or run from a checkout that includes
               <code>frontend-user/index.html</code>.</p>
        </body></html>
        """,
    )
