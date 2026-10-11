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

from app.analyzer import AnalysisError, STORED_FIELDS, analyze, download_photo
from app.clients import NotConfiguredError, StorageWriteError, ensure_table
from app.config import JobStatus, get_settings
from app.job_state import get_job_state, overlay
from app.models import (
    DocumentFieldPatch,
    InvoiceAccepted,
    InvoiceCorrections,
    InvoiceStatusOut,
    ServiceConfig,
)
from app.repository import (
    MAX_FIELD_CHARS,
    ExtractedDocument,
    ExtractedField,
    InvoiceRecord,
    dedupe_key,
    delete_record,
    load_record,
    save_documents,
)
from app.storage import PhotoStorageError, delete_photo, store_photo
from app.usage import UsageError, analyses_this_month, record_analysis

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
            # Count the run against the month's allowance now that it has
            # happened. It is spent whatever the reviewer decides next, so it is
            # recorded before the result is held rather than when it is accepted.
            # A failure to record it is logged and ignored: the reading
            # succeeded, and losing it because the tally could not be written
            # would be worse than an under-count.
            try:
                record_analysis(settings)
            except UsageError as exc:
                logger.warning("Could not record the monthly analysis usage: %s", exc)
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
        # Only the stored fields are ever shown, whatever the source: the
        # analysis keeps exactly this whitelist, but a row may have been written
        # before the filter existed, and the held state forgets nothing it
        # already had.
        body["documents"] = _only_stored_fields(body["documents"])
        # How much of the month's allowance is left. Every analysis run spends
        # one, so this is the same answer whether the result was accepted,
        # rejected or is still on screen. It is read from storage rather than
        # held in the process, because it has to survive a restart to be worth
        # showing at all. A usage table that cannot be read leaves the fields
        # absent, so the UI says nothing rather than inventing a number.
        try:
            used = analyses_this_month(settings)
        except UsageError as exc:
            logger.warning("Could not read the monthly analysis usage: %s", exc)
        else:
            allowance = settings.monthly_document_allowance
            body["monthlyAllowance"] = allowance
            body["documentsUsed"] = used
            body["documentsRemaining"] = max(allowance - used, 0)
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
    async def accept_photo(
        invoice_id: str,
        corrections: InvoiceCorrections | None = None,
    ) -> InvoiceStatusOut:
        """Keep the values, and only now write the record.

        This is the only path in the service that creates rows. The values have
        been on screen unreviewed since the analysis finished, and being told the
        photo was readable is what makes them worth keeping. Until then the
        result lives in process alone, so nothing here can be read back by
        anyone else.

        ``corrections`` carries the values the reviewer entered for fields that
        Document Intelligence read wrong or did not return, merged over the
        extracted reading before the record is written. Omitting it stores the
        reading as it came out of the model.
        """
        _configured()
        partition = invoice_id.strip()
        job_state = get_job_state()
        held = job_state.get(partition)
        record = load_record(settings, partition)
        if held is None and record is None:
            raise HTTPException(status_code=404, detail="Unknown invoice id.")
        if record is None and (held is None or not held.documents):
            # A failed job has no reading, so there is nothing to accept or
            # correct, and storing a blank row would be worse than saying so.
            raise HTTPException(
                status_code=409,
                detail="This invoice has no analysis to accept yet.",
            )

        if corrections is None:
            # A plain confirmation repeats the existing behaviour: write only the
            # reading that has not been stored yet, and leave a stored record
            # alone so a second confirmation is a no-op rather than a rewrite.
            if record is None:
                source = _response_documents(held, None)
                _require_required_fields(source)
                storable = _from_held(source)
            else:
                storable = None
        else:
            # The reviewer corrected values, so the stored record must reflect
            # what they approved. That applies whether the reading is still held
            # or was already stored (and on screen again after a restart).
            source = _response_documents(held, record)
            merged = _apply_corrections(source, corrections)
            storable = _from_held(merged)
            _require_required_fields(merged)
        if storable is not None:
            try:
                save_documents(settings, partition, _blob_name(held, record), storable)
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


def _response_documents(
    held: Any, record: InvoiceRecord | None
) -> list[dict[str, Any]]:
    """Response-shaped documents for whichever source is current.

    A reading that has not been accepted lives in job state; one that has is in
    the table. Both are convertible to the same dict shape, so corrections can
    be applied without caring which source is current.
    """
    if held is not None and held.documents:
        return list(_only_stored_fields(held.documents))
    if record is not None:
        return _only_stored_fields([document.as_dict() for document in record.documents])
    return []


def _only_stored_fields(
    documents: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only the fields this deployment stores, everywhere a value can leave.

    The whitelist already gates extraction and corrections, so this is a guard
    for anything that slipped past them: a row written before the filter
    existed, or a held result that already carried extra fields.
    """
    cleaned: list[dict[str, Any]] = []
    for document in documents:
        document = dict(document)
        fields = document.get("fields") or {}
        document["fields"] = {
            name: value
            for name, value in fields.items()
            if name in STORED_FIELDS
        }
        cleaned.append(document)
    return cleaned


# Fields that must end up in every stored record. Order matters for the error
# message, so a document missing several is told about the first one, the way
# the UI prompts.
REQUIRED_FIELDS = ("VendorName", "InvoiceDate", "InvoiceTotal", "SubTotal", "TotalTax")

# How each required field is named to a person, matching the frontend labels.
REQUIRED_FIELD_LABELS = {
    "VendorName": "merchant",
    "InvoiceDate": "date",
    "InvoiceTotal": "invoice total",
    "SubTotal": "subtotal",
    "TotalTax": "total tax",
}


def _require_required_fields(documents: list[dict[str, Any]]) -> None:
    """Refuse to write a document missing a required field.

    The reviewer is asked for a merchant, a date, the invoice total, the
    subtotal and the total tax until all are read or typed, so a record
    reaching the table always has them. This guard is the last line of that
    rule: a document the model read none from, or whose required value a
    correction cleared, is rejected rather than stored as a blank.
    """
    for position, document in enumerate(documents):
        fields = document.get("fields") or {}
        missing = next(
            (
                name
                for name in REQUIRED_FIELDS
                if not str((fields.get(name) or {}).get("value") or "").strip()
            ),
            None,
        )
        if missing is not None:
            label = REQUIRED_FIELD_LABELS[missing]
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Document {position} has no {label} ({missing}). "
                    f"Correct it before accepting: a {label} is required."
                ),
            )


def _blob_name(held: Any, record: InvoiceRecord | None) -> str:
    """The blob that was analysed, from whichever source is current."""
    if held is not None and held.blob_name:
        return held.blob_name
    if record is not None:
        return record.blob_name
    return ""


def _apply_corrections(
    documents: Sequence[dict[str, Any]], corrections: InvoiceCorrections
) -> list[dict[str, Any]]:
    """Merge reviewer values over an extracted reading.

    Each corrected document is identified by its index, and only the fields it
    names are overridden; a field the reviewer cleared becomes an empty value.
    Anything not mentioned keeps its extracted value, and a correction for a
    page the model did not return is ignored, so a wrong index can never invent
    a document. Corrections may only touch the stored whitelist, so a field
    this deployment keeps nowhere cannot be smuggled back in through one.
    """
    by_index: dict[int, dict[str, DocumentFieldPatch]] = {}
    for patch in corrections.documents:
        by_index[patch.index] = {
            name: field
            for name, field in patch.fields.items()
            if name in STORED_FIELDS
        }
    if not by_index:
        return list(documents)
    merged: list[dict[str, Any]] = []
    for document in documents:
        document = dict(document)
        patch = by_index.get(document.get("index"))
        if patch is not None:
            fields = dict(document.get("fields") or {})
            for name, field in patch.items():
                fields[name] = {"value": field.value}
            document["fields"] = fields
        merged.append(document)
    return merged


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
