"""Admin SPA dashboard.

Serves the single-page admin UI at /admin/ — a self-contained HTML file
that talks to the REST admin API.  Registered in the lifespan AFTER the
REST admin router so API routes take priority.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path

_HTML_PATH = Path(__file__).parent / "static" / "admin" / "index.html"


def mount_admin_ui(app: FastAPI) -> None:
    """Register GET /admin/ serving the SPA HTML (if present)."""
    if not _HTML_PATH.exists():
        return  # Skip silently in dev without the static asset.

    _html_cache: str | None = None
    _html_mtime: float = 0.0

    async def _serve():
        nonlocal _html_cache, _html_mtime
        # Re-read whenever the file changes so a redeploy takes effect without
        # a server restart (the mtime check is cheap; the read is not).
        mtime = _HTML_PATH.stat().st_mtime
        if _html_cache is None or mtime != _html_mtime:
            _html_cache = _HTML_PATH.read_text(encoding="utf-8")
            _html_mtime = mtime
        # no-store: the SPA is a single file whose behaviour changes between
        # releases; a stale cached copy silently keeps old (buggy) JS running.
        return HTMLResponse(
            _html_cache,
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    # Use app.add_api_route so we can order it explicitly — called inside
    # lifespan AFTER include_router(admin_router), the REST routes win.
    app.add_api_route("/admin/", _serve, methods=["GET"], include_in_schema=False)
