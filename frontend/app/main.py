"""Serves the invoice UI.

This service has no storage and no Document Intelligence access. It renders the
page, tells the browser where the analysis backend lives, and does nothing else:
the chosen photo is posted straight to the backend, which owns the Azure side.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Invoice Upload", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/config", include_in_schema=False)
async def api_config() -> dict[str, object]:
    """Everything the UI needs to capture a photo and submit it.

    Deliberately non-secret: there is nothing secret here any more. The upload
    limits are echoed so the UI can offer the right formats in the file chooser
    and reject an unusable file early; the backend enforces the same rules.
    """
    settings = get_settings()
    return {
        "maxUploadBytes": settings.max_upload_bytes,
        "allowedContentTypes": list(settings.allowed_content_types),
        "analysisApiBaseUrl": settings.analysis_api_base_url,
        "analysisConfigured": settings.is_analysis_configured,
    }
