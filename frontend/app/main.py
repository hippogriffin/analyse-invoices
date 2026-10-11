"""Serves the invoice UI, and forwards its API calls to the backend.

This service has no storage and no Document Intelligence access. It renders the
page and proxies the browser's upload/status/verdict calls to the analysis
backend, which owns the Azure side. The browser only ever talks to this service,
so the backend can sit behind a cluster-internal address the browser could not
resolve on its own.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.proxy import router as invoices_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Invoice Upload", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(invoices_router)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    """Served from the root so its scope is the whole app, not /static.

    A service worker may only control URLs at or below its own directory, so the
    one that precaches the page has to live at the top level. A `.js` response
    type is set explicitly; the file is outside the /static mount.
    """
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")



@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/config", include_in_schema=False)
async def api_config() -> dict[str, object]:
    """Everything the UI needs to capture a photo and submit it.

    Deliberately non-secret. The upload limits are echoed so the UI can offer the
    right formats in the file chooser and reject an unusable file early; the
    backend enforces the same rules. The analysis backend's own address is not
    sent: the browser posts to this service, which forwards on, so the address
    can stay internal and the browser has no use for it.
    """
    settings = get_settings()
    return {
        "maxUploadBytes": settings.max_upload_bytes,
        "allowedContentTypes": list(settings.allowed_content_types),
        "analysisConfigured": settings.is_analysis_configured,
    }
