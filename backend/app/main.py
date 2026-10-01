"""Invoice analysis API.

Accept a photo, read its invoice fields with Document Intelligence and keep the
result. The caller sends the image itself and never names a blob, so this service
owns every Azure credential and every blob name.

The photo is a means, not a record. It is deleted once it has been read, whether
the read succeeded or not, so the only thing kept is what was extracted. Table
Storage is written once per invoice, and only when the analysis succeeds, so the
presence of a row means the job finished.

An invoice id comes back immediately and analysis continues in a background task
in the same process, so this stays a single container with no queue. Progress,
failure reasons, confidence and the reviewer's verdict are held in that process
rather than in the table, so a restart loses them: a finished invoice still
reports its values, and a job that was still running is simply unknown afterwards.
See ``app.job_state``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Sequence
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.analyzer import AnalysisError, analyze, download_photo
from app.clients import NotConfiguredError, StorageWriteError, ensure_table
from app.config import JobStatus, get_settings
from app.job_state import get_job_state, overlay
from app.models import (
    InvoiceAccepted,
    InvoiceStatusOut,
    ServiceConfig,
)
from app.repository import (
    MAX_FIELD_CHARS,
    ExtractedDocument,
    ExtractedField,
    dedupe_key,
    delete_record,
    load_record,
    save_documents,
)
from app.storage import PhotoStorageError, delete_photo, store_photo

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 64 * 1024

DESCRIPTION = """
Turns a captured invoice photo into structured fields.

Post the photo itself as multipart form data. The service analyses it with
Document Intelligence, keeps the extracted fields, and deletes the photo once it
has been read.
"""


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for warning in settings.config_warnings():
            logger.warning("Configuration: %s", warning)
        if settings.is_configured:
            try:
                ensure_table(settings)
            except Exception:
                logger.warning(
                    "Could not verify table %s at startup", settings.result_table_name
                )
        yield

    app = FastAPI(
        title="Invoice Analysis",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    def _configured() -> None:
        if not settings.is_configured:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Service is not configured. Set STORAGE_ACCOUNT_URL and "
                    "DOCUMENT_INTELLIGENCE_ENDPOINT."
                ),
            )

    def _discard_source(blob_name: str) -> None:
        """Delete the photo. The extracted fields are the product; this is not.

        Called on every path out of the job, so a photo cannot be left behind
        because the read failed, and cannot be left behind twice either. A
        failure here is logged rather than raised: the analysis result is
        already settled, and reporting a good extraction as failed because a
        cleanup call did not work would be worse than an orphaned blob.
        """
        try:
            delete_photo(settings, blob_name)
        except PhotoStorageError as exc:
            logger.warning(
                "Invoice photo %s could not be deleted: %s", blob_name, exc
            )

    def _fail(state: Any, partition: str, message: str) -> None:
        """Record a failure. Nothing is written to the table, so the reason for
        it lives only as long as the process does."""
        state.fail(partition, message[:MAX_FIELD_CHARS])

    def _run_analysis(partition: str, blob_name: str) -> None:
        """Background work. Never raises: a failure must be recorded as state.

        Runs as a sync function so Starlette executes it in a worker thread,
        which the blocking Azure SDKs need.
        """
        state = get_job_state()
        try:
            state.processing(partition)
            documents = analyze(settings, download_photo(settings, blob_name))
            # Nothing is written here. The values are only worth keeping if the
            # reviewer accepts the reading, so the result waits in memory until
            # they answer, and the table learns nothing about a photo that is
            # still in dispute. See _verdict.
            state.complete(
                partition,
                [document.as_dict() for document in documents],
                blob_name=blob_name,
            )
            logger.info("Invoice %s analysed: %d document(s)", partition, len(documents))
        except (AnalysisError, NotConfiguredError) as exc:
            logger.warning("Invoice %s failed: %s", partition, exc)
            _fail(state, partition, str(exc))
        except Exception:
            logger.exception("Invoice %s failed unexpectedly", partition)
            _fail(
                state, partition, "Analysis failed unexpectedly. Retry the photo."
            )
        finally:
            _discard_source(blob_name)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config", response_model=ServiceConfig)
    def read_config() -> ServiceConfig:
        return ServiceConfig(
            model=settings.document_intelligence_model,
            source_container=settings.source_container,
            result_table=settings.result_table_name,
            max_upload_bytes=settings.max_upload_bytes,
            allowed_content_types=settings.allowed_content_types,
        )

    @app.post(
        "/api/invoices",
        response_model=InvoiceAccepted,
        status_code=202,
        summary="Analyse a photo",
    )
    async def submit(
        background_tasks: BackgroundTasks,
        photo: UploadFile = File(...),
    ) -> InvoiceAccepted:
        _configured()

        content_type = (photo.content_type or "").split(";")[0].strip().lower()
        if content_type not in settings.allowed_content_types:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported image type {content_type or 'unknown'}. "
                    f"Allowed: {', '.join(settings.allowed_content_types)}."
                ),
            )

        data = await _read_limited(photo, settings.max_upload_bytes)
        if not data:
            raise HTTPException(
                status_code=400, detail="The uploaded file was empty."
            )

        # The invoice id is the idempotency key, derived from the bytes. An
        # identical photo that is already known is not analysed again, because
        # Document Intelligence bills per page.
        partition = dedupe_key(data)
        state = get_job_state()
        if load_record(settings, partition) is not None:
            logger.info("Invoice %s already stored; not analysing again", partition)
            return InvoiceAccepted(
                invoice_id=partition,
                status_url=f"/api/invoices/{partition}",
                already_processed=True,
            )

        # Already read, and nobody has rejected it yet. Analysing it again would
        # bill for a page that has been paid for once already. It is reported as
        # fresh, because a held result is not "already processed" from the
        # reviewer's side: there is something still waiting to be accepted, and
        # the status endpoint will hand it straight back.
        held = state.get(partition)
        if held is not None and held.documents:
            logger.info("Invoice %s already read and unconfirmed", partition)
            return InvoiceAccepted(
                invoice_id=partition,
                status_url=f"/api/invoices/{partition}",
                already_processed=False,
            )

        # Claiming is a single atomic step, and it happens before the blob write
        # rather than after it. The write is awaited, so claiming afterwards left
        # a window in which a second request for the same photo saw an unclaimed
        # invoice and queued a second billable analysis.
        if not state.claim(partition):
            logger.info("Invoice %s is already being analysed", partition)
            return InvoiceAccepted(
                invoice_id=partition,
                status_url=f"/api/invoices/{partition}",
                already_processed=True,
            )

        # The name is derived from the content too, so the write is idempotent
        # and the container does not collect near-duplicate blobs. The photo is
        # deleted once the job finishes, one way or another.
        #
        # The claim is released if the write fails, so a transient storage error
        # does not leave the photo permanently unanalysable.
        try:
            stored = await run_in_threadpool(store_photo, data, content_type, settings)
        except PhotoStorageError as exc:
            state.forget(partition)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        background_tasks.add_task(_run_analysis, partition, stored.name)

        return InvoiceAccepted(
            invoice_id=partition,
            status_url=f"/api/invoices/{partition}",
            already_processed=False,
        )

    def _response(partition: str) -> InvoiceStatusOut:
        """Compose the status response from storage plus in-process state.

        A stored record means the reviewer accepted the reading, so a partition
        with rows is ``complete`` and settled whatever the process remembers. A
        completed analysis whose rows have not been written yet is reported from
        memory, so the reviewer can be shown the values and asked about them
        before anything is kept. Everything else is taken from the job state, and
        is ``None`` after a restart rather than invented.
        """
        record = load_record(settings, partition)
        state = get_job_state().get(partition)
        if record is None and state is None:
            raise HTTPException(status_code=404, detail="Unknown invoice id.")
        if record is not None:
            body = record.as_response()
            body["status"] = JobStatus.COMPLETE.value
            body["documents"] = overlay(partition, body["documents"])
            # A record exists only because the reading was accepted, so this is
            # still true after a restart that forgot the verdict itself. Without
            # that, a restart would ask about a photo that was already answered.
            satisfied: bool | None = True
        elif state.documents:
            # Read, on screen, and not yet accepted: reported so the reviewer can
            # accept or reject it. Nothing has been written.
            body = {
                "invoiceId": partition,
                "blobName": state.blob_name,
                "status": JobStatus.COMPLETE.value,
                "createdAt": "",
                "completedAt": None,
                "documentCount": len(state.documents),
                "documents": state.documents,
            }
            satisfied = state.satisfied
        else:
            body = {
                "invoiceId": partition,
                "blobName": "",
                "status": state.status.value,
                "createdAt": "",
                "completedAt": None,
                "documentCount": 0,
                "documents": [],
            }
            satisfied = state.satisfied
        body["error"] = state.error if state else None
        body["photoSatisfied"] = satisfied
        return InvoiceStatusOut.model_validate(body)

    @app.get(
        "/api/invoices/{invoice_id}",
        response_model=InvoiceStatusOut,
        summary="Check analysis status and read extracted fields",
    )
    def status(invoice_id: str) -> InvoiceStatusOut:
        _configured()
        return _response(invoice_id.strip())

    @app.post(
        "/api/invoices/{invoice_id}/photo",
        response_model=InvoiceStatusOut,
        summary="Accept the reading, and store the invoice",
    )
    async def accept_photo(invoice_id: str) -> InvoiceStatusOut:
        """Keep the values, and only now write the record.

        This is the only path in the service that creates rows. The values have
        been on screen unreviewed since the analysis finished, and being told the
        photo was readable is what makes them worth keeping. Until then the
        result lives in process alone, so nothing here can be read back by
        anyone else.
        """
        _configured()
        partition = invoice_id.strip()
        job_state = get_job_state()
        held = job_state.get(partition)
        record = load_record(settings, partition)
        if held is None and record is None:
            raise HTTPException(status_code=404, detail="Unknown invoice id.")
        if record is None:
            if not held.documents:
                raise HTTPException(
                    status_code=409,
                    detail="This invoice has no analysis to accept yet.",
                )
            # Reporting a storage failure as a failed analysis would be a lie
            # about the photo, so it is passed on as the upstream problem it is.
            try:
                save_documents(
                    settings, partition, held.blob_name, _from_held(held.documents)
                )
            except StorageWriteError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        job_state.set_satisfied(partition, True)
        return _response(partition)

    @app.delete(
        "/api/invoices/{invoice_id}/photo",
        status_code=204,
        response_class=Response,
        summary="Reject the photo, and keep nothing",
    )
    async def retake_photo(invoice_id: str) -> Response:
        """Throw the reading away, so nothing is stored for this photo.

        Any stored record goes as well. A record exists only because a reading
        was accepted, so a reviewer saying the photo was not readable is saying
        the values are not to be trusted. That includes a record accepted before
        a restart, which the transient verdict has since forgotten.

        There is deliberately no response body: the invoice is gone, and there is
        nothing for the caller to render.
        """
        _configured()
        partition = invoice_id.strip()
        job_state = get_job_state()
        held = job_state.get(partition)
        record = load_record(settings, partition)
        if held is None and record is None:
            raise HTTPException(status_code=404, detail="Unknown invoice id.")
        if record is not None:
            try:
                delete_record(settings, partition)
            except StorageWriteError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        job_state.forget(partition)
        return Response(status_code=204)

    return app


def _from_held(documents: Sequence[dict[str, Any]]) -> list[ExtractedDocument]:
    """Rebuild the storable documents from a result held in job state.

    Job state keeps a result in the shape the API returns, so the reviewer can be
    shown the confidence that the table deliberately omits. Writing it back needs
    the values alone. The round trip drops exactly that confidence, which is the
    one thing a row should never have carried.
    """
    restored: list[ExtractedDocument] = []
    for position, document in enumerate(documents):
        fields = [
            ExtractedField(name=name, value=item.get("value"))
            for name, item in (document.get("fields") or {}).items()
            if isinstance(item, dict)
        ]
        index = document.get("index")
        restored.append(
            ExtractedDocument(
                index=index if isinstance(index, int) else position,
                fields=fields,
            )
        )
    return restored


async def _read_limited(photo: UploadFile, max_bytes: int) -> bytes:
    """Read the upload in chunks, refusing anything over ``max_bytes``.

    The limit is applied while reading rather than from a declared length, so an
    oversized body is refused without ever being buffered in full.
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


app = create_app()
