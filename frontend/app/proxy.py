"""Server-side proxy to the invoice analysis backend.

The browser used to be handed the backend's address in ``/api/config`` and post
to it directly. That only works when the address means something to the browser,
which a cluster-internal Kubernetes service name does not: it resolves only
inside the cluster. Forwarding the calls here keeps the browser on this
service's own origin and lets ``ANALYSIS_API_BASE_URL`` be any address *this*
process can reach, including an in-cluster service name. It also drops the
browser CORS requirement, since the calls are no longer cross-origin.

The proxy does not interpret the traffic. It copies the method, path and body to
the backend and hands the status, body and content type back unchanged, so the
backend stays the single place that validates a photo and owns the result.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

# The backend stores the photo and answers immediately, because the analysis
# runs in a background task, so no single call here is long. The write timeout
# is generous anyway: a large photo over a slow link still has to arrive first.
PROXY_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=60.0, write=120.0)

READ_CHUNK_BYTES = 64 * 1024

# The only response header worth carrying back. Content-Type is what lets the
# browser parse a JSON body, success or error; the rest are hop-by-hop or are
# generated per request by this server.
_FORWARDED_RESPONSE_HEADERS = ("content-type",)


def _target(path: str) -> str:
    settings = get_settings()
    if not settings.is_analysis_configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "No analysis service is configured (ANALYSIS_API_BASE_URL), so "
                "there is nowhere to send this photo."
            ),
        )
    return f"{settings.analysis_api_base_url}{path}"


async def _forward(
    method: str,
    path: str,
    *,
    files: dict | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """Send one request to the backend and hand its answer back unchanged."""
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            upstream = await client.request(
                method, _target(path), files=files, content=content, headers=headers
            )
    except httpx.HTTPError as exc:
        # A backend that cannot be reached is a gateway problem, not a bad
        # request: 502 tells the UI the photo may be retried rather than
        # implying the photo itself was rejected.
        logger.warning("Analysis backend request %s %s failed: %s", method, path, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the analysis service: {exc}",
        ) from exc

    headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in _FORWARDED_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


async def _read_limited(photo: UploadFile, max_bytes: int) -> bytes:
    """Buffer the upload, refusing anything over the configured limit.

    The backend applies the same limit authoritatively. Enforcing it here too
    keeps this process from holding a body it is only going to be told to
    reject, so the proxy cannot be used to make the frontend buffer without
    bound.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await photo.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Image is larger than the {max_bytes} byte limit.",
            )
        chunks.append(chunk)
    await photo.close()
    return b"".join(chunks)


@router.post("", status_code=202, summary="Analyse a photo")
async def submit(photo: UploadFile = File(...)) -> Response:
    settings = get_settings()
    data = await _read_limited(photo, settings.max_upload_bytes)
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    files = {
        "photo": (
            photo.filename or "invoice.jpg",
            data,
            photo.content_type or "application/octet-stream",
        )
    }
    return await _forward("POST", "/api/invoices", files=files)


@router.get("/{invoice_id}", summary="Check analysis status and read fields")
async def status(invoice_id: str) -> Response:
    return await _forward("GET", f"/api/invoices/{invoice_id}")


@router.post("/{invoice_id}/photo", summary="Accept the reading")
async def accept_photo(invoice_id: str, request: Request) -> Response:
    """Accept the reading, forwarding any reviewer values unchanged.

    The browser sends the values the reviewer changed or added as a JSON body
    when the reading was edited. This is forwarded byte for byte: this process
    does not interpret the corrections, so it only has to copy them across.
    """
    content = (await request.body()) or None
    headers = {"content-type": request.headers.get("content-type")} if content else None
    return await _forward(
        "POST", f"/api/invoices/{invoice_id}/photo", content=content, headers=headers
    )


@router.delete("/{invoice_id}/photo", summary="Reject the photo")
async def retake_photo(invoice_id: str) -> Response:
    return await _forward("DELETE", f"/api/invoices/{invoice_id}/photo")
