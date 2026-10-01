"""Extracted invoice fields, stored in Table Storage.

The table holds the result of an analysis and nothing else. A row is written when
a photo has been read successfully, and a photo that was never read, or that
failed to read, leaves nothing behind. Progress, failure reasons, confidence and
the reviewer's verdict are all deliberately not stored here; see
``app.job_state`` for why, and where they live instead.

A Table Storage transaction is atomic but capped at 100 entities in a single
partition, so the design puts one partition per invoice:

* ``doc-000`` - the first recognised document, and the marker that this invoice
  finished: it carries the invoice-level columns as well as its own fields
* ``doc-001`` - the second page, and so on

There is no separate ``meta`` row. ``doc-000`` is that row, because the analyzer
never returns zero documents (``app.analyzer`` raises instead), so the first page
is always there to carry the marker. Folding them together keeps the read a
single point get on a key the service already knows, and it means a partition
holds nothing but pages of the invoice.

Everything for an invoice fits in one partition and one transaction, so a reader
never sees a page without the marker that says the analysis finished. The
partition key is derived from the photo's content hash, so re-submitting the same
photo overwrites the same rows instead of appending duplicates. This matters
because Document Intelligence bills per page: a retried analysis costs money, and
a duplicate row is a duplicate invoice.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import FIRST_DOC_ROW_KEY, Settings
from app.clients import StorageWriteError, azure_error_message, table_client

logger = logging.getLogger(__name__)

MAX_FIELD_CHARS = 1024

#: Row key prefix for a document page. The suffix is the zero-padded page number,
#: which is what carries the page order now that there is no index column.
DOC_ROW_PREFIX = "doc-"

TABLE_HINT = (
    "Check STORAGE_ACCOUNT_URL: it must be a storage account blob endpoint "
    "(https://<account>.blob.core.windows.net); the Table endpoint is derived "
    "from the account name, and the identity needs Storage Table Data Contributor."
)


MAX_TRANSACTION_ENTITIES = 100


def _write(action: str, operation: Any, /, **kwargs: Any) -> Any:
    """Run a Table Storage write, naming the operation if it fails.

    Without this the traceback from Azure says only that some verb was wrong,
    which does not identify which of the several table calls went wrong.
    """
    try:
        return operation(**kwargs)
    except Exception as exc:
        raise StorageWriteError(
            azure_error_message(exc, action, TABLE_HINT)
        ) from exc


def _submit(
    table: Any,
    action: str,
    operations: list[tuple[str, dict[str, Any]]],
) -> None:
    """Write several rows atomically, chunked to the service transaction limit.

    ``TableClient.upsert_entity`` accepts a single entity; batching only exists
    on ``submit_transaction``.
    """
    for start in range(0, len(operations), MAX_TRANSACTION_ENTITIES):
        chunk = operations[start : start + MAX_TRANSACTION_ENTITIES]
        _write(
            action,
            table.submit_transaction,
            operations=chunk,
        )


def dedupe_key(photo: bytes) -> str:
    """Stable partition key for a photo's content.

    The caller no longer names the blob, so the id comes from the bytes. That
    makes it idempotent in a stronger sense than the old blob-name + ETag pair:
    the same photo is the same invoice no matter how it was submitted.
    """
    return hashlib.sha256(photo).hexdigest()[:32]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def coerce(value: Any) -> Any:
    """Make a value safe for Table Storage.

    Table entities reject ``None``, and the service returns a mix of strings,
    numbers, dates, currency objects and nested objects. Everything becomes a
    string, number or bool so a surprising model response cannot fail the write.
    """
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime | str):
        text = value.isoformat() if isinstance(value, datetime) else value
        return text[:MAX_FIELD_CHARS]
    amount = getattr(value, "amount", None)
    if amount is not None:
        code = getattr(value, "code", "") or ""
        return f"{amount} {code}".strip()[:MAX_FIELD_CHARS]
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)[:MAX_FIELD_CHARS]
    if isinstance(value, dict):
        pairs = (f"{key}={val}" for key, val in value.items())
        return "; ".join(pairs)[:MAX_FIELD_CHARS]
    return str(value)[:MAX_FIELD_CHARS]


@dataclass
class ExtractedField:
    name: str
    value: Any
    #: Carried through the API for the reviewer's benefit, never written to a
    #: row. It comes from the in-process result, so it is absent for a record
    #: that was read back after a restart.
    confidence: float | None = None


@dataclass
class ExtractedDocument:
    #: Page order. This is not a column: it is recovered from the row key on the
    #: way back in, so the order survives without being stored twice.
    index: int
    #: In-memory only, as above.
    confidence: float | None = None
    fields: list[ExtractedField] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "confidence": self.confidence,
            "fields": {
                item.name: {"value": item.value, "confidence": item.confidence}
                for item in self.fields
            },
        }


@dataclass
class InvoiceRecord:
    """What a successful analysis left behind.

    There is no status, error or verdict here, because nothing is written to the
    table until an analysis succeeds. A record therefore always describes a
    finished extraction; how the job was getting on, and what the reviewer
    thought of the photo, live in ``app.job_state``.
    """

    invoice_id: str
    blob_name: str
    created_at: str
    completed_at: str | None = None
    document_count: int = 0
    documents: list[ExtractedDocument] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "invoiceId": self.invoice_id,
            "blobName": self.blob_name,
            "createdAt": self.created_at,
            "completedAt": self.completed_at,
            "documentCount": self.document_count,
            "documents": [doc.as_dict() for doc in self.documents],
        }


def save_documents(
    settings: Settings,
    partition: str,
    blob_name: str,
    documents: list[ExtractedDocument],
) -> None:
    """Write the result of a successful analysis: one row per document.

    This is the only write in the service. Nothing is written while a job is
    running or after it fails, so a row in the table means exactly one thing: this
    photo was read and these are the values that came out of it.

    The first page carries the invoice-level columns too. It is the marker that
    the analysis finished, which is why the read is a point get on a row key the
    service already knows rather than a scan of the partition.

    Rows from a previous run for the same photo are replaced, not duplicated: a
    stale ``doc-NNN`` beyond the new document count is removed so a re-run with
    fewer pages does not leave old results behind. Every row belongs to the
    invoice's partition and goes in one transaction, so a reader can never see
    extracted fields without the marker. Table Storage caps a transaction at 100
    entities, so the operations are chunked.
    """
    table = table_client(settings)
    timestamp = now_iso()
    # Written onto the first page, which is the marker row. See the module
    # docstring for why there is no separate meta row.
    marker = {
        "invoiceId": partition,
        # Kept as an audit trail of what was read. The blob itself is deleted
        # once processing finishes, so this names a photo that no longer exists.
        "blobName": blob_name,
        "createdAt": timestamp,
        "completedAt": timestamp,
        "documentCount": len(documents),
    }

    entities: list[dict[str, Any]] = []
    for doc in documents:
        # The row key carries the page order, so no index column is needed, and
        # neither confidence nor docType is written: a score is a live reading
        # aid rather than a durable fact, and the type is not used downstream.
        # See app.job_state.
        entity: dict[str, Any] = {
            "PartitionKey": partition,
            "RowKey": f"{DOC_ROW_PREFIX}{doc.index:03d}",
        }
        if doc.index == 0:
            entity.update(marker)
        for item in doc.fields:
            entity[f"f_{item.name}"] = coerce(item.value)
        entities.append(entity)

    existing = _existing_row_keys(table, partition)
    operations: list[tuple[str, dict[str, Any]]] = [
        ("upsert", entity) for entity in entities
    ]
    stale = [
        {"PartitionKey": partition, "RowKey": key}
        for key in existing - {entity["RowKey"] for entity in entities}
    ]
    operations.extend(("delete", entity) for entity in stale)

    _submit(
        table,
        "save the extracted fields",
        operations,
    )


def delete_record(settings: Settings, partition: str) -> None:
    """Remove every row for an invoice.

    The other half of :func:`save_documents`. A record only exists because a
    reviewer accepted a reading, so rejecting that reading has to take the record
    back out: a rejected photo must leave nothing behind that looks like a kept
    invoice.

    Every row is deleted, not just the marker. Leaving ``doc-001+`` behind would
    keep the values the reviewer rejected, and would make a later
    :func:`load_record` fail on a missing marker while stale fields sat in the
    partition.
    """
    table = table_client(settings)
    rows = [
        {"PartitionKey": partition, "RowKey": entity["RowKey"]}
        for entity in _partition_entities(table, partition)
    ]
    if not rows:
        return
    for start in range(0, len(rows), MAX_TRANSACTION_ENTITIES):
        chunk = rows[start : start + MAX_TRANSACTION_ENTITIES]
        _write(
            "delete the stored invoice",
            table.submit_transaction,
            operations=[("delete", row) for row in chunk],
        )


def _partition_entities(table: Any, partition: str) -> list[dict[str, Any]]:
    """Every row in one partition.

    ``TableClient.list_entities`` has no ``partition_key`` argument, so a
    partition scan has to be an OData query. The key is passed as a parameter
    rather than interpolated, because partition keys may contain a quote.
    """
    rows = table.query_entities(
        "PartitionKey eq @partition_key", parameters={"partition_key": partition}
    )
    return [dict(entity) for entity in rows]


def _existing_row_keys(table: Any, partition: str) -> set[str]:
    keys: set[str] = set()
    try:
        for entity in _partition_entities(table, partition):
            keys.add(entity["RowKey"])
    except Exception:
        logger.debug("Could not list existing rows for partition %s", partition)
    return keys


def load_record(settings: Settings, partition: str) -> InvoiceRecord | None:
    """The stored result for an invoice, or ``None`` if it never succeeded.

    A record exists only once an analysis has completed, so there is no status to
    read back: finding the first document *is* the answer to "did this finish?".
    That is a point get on a key the service already knows, which is cheaper than
    scanning the partition, and it doubles as the source of the invoice-level
    columns.
    """
    table = table_client(settings)
    try:
        first = table.get_entity(
            partition_key=partition, row_key=FIRST_DOC_ROW_KEY
        )
    except Exception:
        return None

    record = InvoiceRecord(
        invoice_id=first.get("invoiceId") or partition,
        blob_name=first.get("blobName") or "",
        created_at=first.get("createdAt") or "",
        completed_at=first.get("completedAt") or None,
        document_count=int(first.get("documentCount") or 0),
    )
    record.documents = _load_documents(table, partition, record.document_count)
    return record


def _load_documents(
    table: Any, partition: str, expected: int
) -> list[ExtractedDocument]:
    documents: list[ExtractedDocument] = []
    for entity in sorted(
        _partition_entities(table, partition), key=lambda item: item["RowKey"]
    ):
        row_key = entity["RowKey"]
        if not row_key.startswith(DOC_ROW_PREFIX):
            continue
        fields = [
            ExtractedField(name=key[2:], value=value)
            for key, value in entity.items()
            if key.startswith("f_")
        ]
        documents.append(
            ExtractedDocument(
                # The page order lives in the row key rather than in a column.
                index=_page_index(row_key),
                fields=fields,
            )
        )
    return documents[:expected] if expected else documents


def _page_index(row_key: str) -> int:
    """Recover the page number from a ``doc-NNN`` key.

    Zero-padded keys sort correctly as text, and this keeps the order identical
    to the order the pages were analysed in. A key that is not in that shape
    sorts first rather than raising, so a hand-edited row cannot break a read.
    """
    suffix = row_key[len(DOC_ROW_PREFIX) :]
    try:
        return int(suffix)
    except ValueError:
        return 0
