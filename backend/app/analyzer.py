"""Download a captured photo and read its invoice fields.

The field walk is generic on purpose: prebuilt-invoice returns a different
field set per industry, and a hardcoded list would silently drop fields. Every
field the model returns is captured, with its confidence, so adding a field
never needs a code change.
"""

from __future__ import annotations

import logging

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from app.clients import (
    NotConfiguredError,
    azure_error_message,
    blob_service,
    document_intelligence_credential,
)
from app.config import Settings
from app.repository import ExtractedDocument, ExtractedField, coerce

logger = logging.getLogger(__name__)

MAX_BLOB_BYTES = 50 * 1024 * 1024

DI_ENDPOINT_HINT = (
    "Check DOCUMENT_INTELLIGENCE_ENDPOINT: it must be the Document Intelligence "
    "resource endpoint (https://<resource>.cognitiveservices.azure.com), not the "
    "storage account, search, or AI Foundry model endpoint."
)

# Ordered by specificity: a currency field must be read before the generic
# number accessor, and a selection group before the raw content fallback.
_VALUE_ATTRIBUTES = (
    "value_currency",
    "value_address",
    "value_phone_number",
    "value_country_region",
    "value_time",
    "value_date",
    "value_selection_group",
    "value_number",
    "value_integer",
    "value_string",
    "value_boolean",
)


class AnalysisError(RuntimeError):
    """Analysis failed in a way worth surfacing to the caller."""


def _read_field(field: object) -> tuple[object, float | None]:
    for attribute in _VALUE_ATTRIBUTES:
        value = getattr(field, attribute, None)
        if value is not None:
            return value, getattr(field, "confidence", None)
    # Fall back to the raw text span so a field is never silently lost.
    return getattr(field, "content", "") or "", getattr(field, "confidence", None)


def _to_documents(result: object) -> list[ExtractedDocument]:
    documents: list[ExtractedDocument] = []
    for index, document in enumerate(getattr(result, "documents", None) or []):
        items: list[ExtractedField] = []
        for name, field in sorted(
            (getattr(document, "fields", None) or {}).items()
        ):
            raw_value, confidence = _read_field(field)
            items.append(
                ExtractedField(name=name, value=coerce(raw_value), confidence=confidence)
            )
        documents.append(
            ExtractedDocument(
                index=index,
                confidence=getattr(document, "confidence", None),
                fields=items,
            )
        )
    return documents


def download_photo(settings: Settings, blob_name: str) -> bytes:
    # The blob name is derived from the content, so it is always in scope. The
    # check guards the one path where it comes from elsewhere: a row this
    # service did not write, which could name a blob outside the container.
    if not settings.accepts_blob(blob_name):
        raise AnalysisError(
            f"Refusing to read a photo outside "
            f"{settings.source_container}"
            + (f"/{settings.source_prefix}" if settings.source_prefix else "")
            + "."
        )
    try:
        blob = blob_service(settings).get_blob_client(
            container=settings.source_container, blob=blob_name
        )
        # BlobClient.download_blob has no max_single_get_size option, so the cap
        # is enforced by asking for one byte more than is allowed and refusing
        # anything longer. A truncated download is never used. length requires
        # offset to be set as well.
        stream = blob.download_blob(offset=0, length=MAX_BLOB_BYTES + 1)
        photo = stream.readall()
    except ResourceNotFoundError as exc:
        raise AnalysisError(f"Photo not found in {settings.source_container}.") from exc
    except HttpResponseError as exc:
        raise AnalysisError(
            azure_error_message(exc, "read the photo from blob storage")
        ) from exc
    if len(photo) > MAX_BLOB_BYTES:
        raise AnalysisError(
            f"Photo is larger than the {MAX_BLOB_BYTES // (1024 * 1024)} MB limit."
        )
    return photo


def analyze(settings: Settings, photo: bytes) -> list[ExtractedDocument]:
    if not settings.document_intelligence_endpoint:
        raise NotConfiguredError("DOCUMENT_INTELLIGENCE_ENDPOINT is not set.")
    client = DocumentIntelligenceClient(
        endpoint=settings.document_intelligence_endpoint,
        credential=document_intelligence_credential(settings),
    )
    try:
        poller = client.begin_analyze_document(
            settings.document_intelligence_model, photo
        )
        result = poller.result(timeout=settings.analysis_timeout_seconds)
    except HttpResponseError as exc:
        # Most often a 429 when the free tier rate limit is exceeded, or a
        # 401/403 when the identity lacks Cognitive Services User.
        raise AnalysisError(
            azure_error_message(exc, "analyse the photo", DI_ENDPOINT_HINT)
        ) from exc
    except TimeoutError as exc:
        raise AnalysisError(
            f"Analysis exceeded {settings.analysis_timeout_seconds}s."
        ) from exc

    documents = _to_documents(result)
    if not documents:
        raise AnalysisError(
            "No invoice was recognised. The photo may be blurred, cropped "
            "or not an invoice."
        )
    return documents
