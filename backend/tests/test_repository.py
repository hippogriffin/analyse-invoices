"""Result storage, the column set, and idempotency."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.repository import (
    ExtractedDocument,
    ExtractedField,
    coerce,
    dedupe_key,
    load_record,
    save_documents,
)

def _own_columns(entity: dict) -> dict:
    """The properties the service wrote, without the service's own metadata."""
    return {k: v for k, v in entity.items() if not k.startswith("odata.")}


PARTITION = "partition-under-test"
BLOB = "2026/abc123.jpg"
PHOTO = b"\xff\xd8\xff-not-a-real-jpeg"


class Currency:
    def __init__(self, amount: float, code: str) -> None:
        self.amount = amount
        self.code = code


def test_dedupe_key_is_stable() -> None:
    assert dedupe_key(PHOTO) == dedupe_key(PHOTO)


def test_dedupe_key_changes_when_the_photo_changes() -> None:
    assert dedupe_key(PHOTO) != dedupe_key(b"a different photo")


def test_dedupe_key_is_the_sha256_of_the_bytes() -> None:
    """Pinned so the id stays derived from the content and nothing else."""
    import hashlib

    assert dedupe_key(PHOTO) == hashlib.sha256(PHOTO).hexdigest()[:32]


def test_dedupe_key_is_short_enough_for_a_partition_key() -> None:
    assert len(dedupe_key(PHOTO)) == 32


class TestCoerce:
    def test_none_becomes_empty_string(self) -> None:
        # Table Storage rejects None outright.
        assert coerce(None) == ""

    def test_numbers_and_booleans_pass_through(self) -> None:
        assert coerce(42) == 42
        assert coerce(4.25) == 4.25
        assert coerce(True) is True

    def test_currency_combines_amount_and_code(self) -> None:
        assert coerce(Currency(1234.5, "EUR")) == "1234.5 EUR"

    def test_datetime_becomes_iso_string(self) -> None:
        moment = datetime(2026, 3, 1, tzinfo=UTC)
        assert coerce(moment) == "2026-03-01T00:00:00+00:00"

    def test_lists_and_dicts_become_readable_text(self) -> None:
        assert coerce(["a", "b"]) == "a, b"
        assert coerce({"k": "v"}) == "k=v"

    def test_long_values_are_truncated(self) -> None:
        assert len(coerce("x" * 5000)) == 1024


def test_load_record_returns_none_for_unknown_partition(settings) -> None:
    assert load_record(settings, "nope") is None


def test_nothing_is_written_before_an_analysis_succeeds(settings, table) -> None:
    """There is no pending row to write: absence of a row means "not finished".

    The service has no code that creates a partition before a result exists, so
    this asserts the property from the other side -- after a result is saved the
    partition exists, and there is no other way in.
    """
    assert load_record(settings, PARTITION) is None
    assert table.entities == {}

    save_documents(settings, PARTITION, BLOB, [ExtractedDocument(index=0, fields=[])])

    assert (PARTITION, "doc-000") in table.entities


class TestColumnSet:
    """The table holds a result and nothing else.

    These pin the exact columns, because a table that quietly grows a status or a
    verdict column is how a "transient" field becomes a stored one, and how an
    empty-looking page turns out to disagree with its neighbours about what
    happened to the invoice.
    """

    FORBIDDEN = {
        "status",
        "error",
        "photoSatisfied",
        "photoRetained",
        "docType",
        "index",
        "updatedAt",
    }

    def test_a_later_page_holds_only_keys_and_fields(
        self, settings, table
    ) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(
                    index=0,
                    confidence=0.98,
                    fields=[ExtractedField("VendorName", "Acme", 0.99)],
                ),
                ExtractedDocument(
                    index=1,
                    confidence=0.91,
                    fields=[
                        ExtractedField("VendorName", "Acme", 0.99),
                        ExtractedField("InvoiceTotal", "120.00 EUR", 0.95),
                    ],
                ),
            ],
        )
        doc = _own_columns(table.entities[(PARTITION, "doc-001")])
        assert set(doc) == {
            "PartitionKey",
            "RowKey",
            "f_VendorName",
            "f_InvoiceTotal",
        }

    def test_the_first_page_carries_the_finished_marker(
        self, settings, table
    ) -> None:
        save_documents(
            settings, PARTITION, BLOB, [ExtractedDocument(index=0, fields=[])]
        )
        first = _own_columns(table.entities[(PARTITION, "doc-000")])
        assert not self.FORBIDDEN & set(first), (
            f"removed columns came back: {sorted(self.FORBIDDEN & set(first))}"
        )
        assert set(first) == {
            "PartitionKey",
            "RowKey",
            "invoiceId",
            "blobName",
            "createdAt",
            "completedAt",
            "documentCount",
        }

    def test_later_pages_do_not_repeat_the_marker(
        self, settings, table
    ) -> None:
        """The marker is one row's job. Repeating it on every page would put the
        same invoice-level value in several rows that could disagree."""
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(index=0, fields=[]),
                ExtractedDocument(index=1, fields=[ExtractedField("A", "1")]),
            ],
        )
        second = _own_columns(table.entities[(PARTITION, "doc-001")])
        assert set(second) == {"PartitionKey", "RowKey", "f_A"}

    def test_no_meta_row_exists(self, settings, table) -> None:
        """The marker was folded into doc-000; a leftover meta row would be a
        second place for the invoice-level columns to live."""
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(index=0, fields=[]),
                ExtractedDocument(index=1, fields=[]),
            ],
        )
        assert not [k for k in table.entities if k[1] == "meta"]

    def test_the_marker_and_its_own_fields_share_one_row(
        self, settings, table
    ) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(
                    index=0, fields=[ExtractedField("VendorName", "Acme")]
                )
            ],
        )
        first = _own_columns(table.entities[(PARTITION, "doc-000")])
        assert first["f_VendorName"] == "Acme"
        assert first["invoiceId"] == PARTITION

    def test_no_row_anywhere_carries_a_removed_column(
        self, settings, table
    ) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(index=0, fields=[ExtractedField("A", "1")]),
                ExtractedDocument(index=1, fields=[]),
            ],
        )
        for (partition, row_key), entity in table.entities.items():
            leaked = self.FORBIDDEN & set(_own_columns(entity))
            assert not leaked, f"{partition}/{row_key} still has {sorted(leaked)}"


class TestPageOrderComesFromTheRowKey:
    """There is no index column, so the order must survive being derived."""

    def test_pages_are_read_back_in_analysis_order(
        self, settings, table
    ) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(index=2, fields=[ExtractedField("Page", "three")]),
                ExtractedDocument(index=10, fields=[ExtractedField("Page", "eleven")]),
                ExtractedDocument(index=0, fields=[ExtractedField("Page", "one")]),
            ],
        )
        record = load_record(settings, PARTITION)
        assert record is not None
        assert [doc.index for doc in record.documents] == [0, 2, 10]
        assert [doc.fields[0].value for doc in record.documents] == [
            "one",
            "three",
            "eleven",
        ]

    def test_a_hand_edited_key_does_not_break_the_read(
        self, settings, table
    ) -> None:
        """A row that is not in doc-NNN shape must not raise on read."""
        save_documents(
            settings, PARTITION, BLOB, [ExtractedDocument(index=0, fields=[])]
        )
        table.entities[(PARTITION, "doc-abc")] = {
            "PartitionKey": PARTITION,
            "RowKey": "doc-abc",
            "f_VendorName": "Acme",
        }
        record = load_record(settings, PARTITION)
        assert record is not None
        assert len(record.documents) == 1


class TestSaveDocuments:
    def test_writes_a_row_per_document(self, settings) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(
                    index=0,
                    confidence=0.98,
                    fields=[
                        ExtractedField("VendorName", "Acme", 0.99),
                        ExtractedField("InvoiceTotal", "120.00 EUR", 0.95),
                    ],
                ),
                ExtractedDocument(index=1, fields=[]),
            ],
        )
        record = load_record(settings, PARTITION)
        assert record is not None
        assert record.document_count == 2
        assert [doc.index for doc in record.documents] == [0, 1]
        first = record.documents[0]
        assert {item.name: item.value for item in first.fields} == {
            "VendorName": "Acme",
            "InvoiceTotal": "120.00 EUR",
        }

    def test_no_confidence_is_written_to_any_row(self, settings, table) -> None:
        """Confidence is a review aid, not a stored fact.

        The requirement is that the table holds no scores at all, so this checks
        every column of every row rather than the response, which could be
        showing a score supplied from memory.
        """
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(
                    index=0,
                    confidence=0.98,
                    fields=[
                        ExtractedField("VendorName", "Acme", 0.99),
                        ExtractedField("InvoiceTotal", "120.00 EUR", 0.95),
                    ],
                )
            ],
        )
        stored = {
            name
            for entity in table.entities.values()
            for name in entity
            if name.lower().startswith("c_") or name.lower() == "confidence"
        }
        assert stored == set(), f"confidence persisted as {sorted(stored)}"
        # The values themselves must still be there.
        doc = table.entities[(PARTITION, "doc-000")]
        assert doc["f_VendorName"] == "Acme"
        assert doc["f_InvoiceTotal"] == "120.00 EUR"

    def test_rerun_is_idempotent(self, settings, table) -> None:
        documents = [
            ExtractedDocument(
                index=0, fields=[ExtractedField("VendorName", "Acme", 0.99)]
            )
        ]
        save_documents(
            settings,
            PARTITION,
            BLOB,
            documents)
        before = len(table.entities)
        save_documents(
            settings,
            PARTITION,
            BLOB,
            documents)
        save_documents(
            settings,
            PARTITION,
            BLOB,
            documents)
        assert len(table.entities) == before
        record = load_record(settings, PARTITION)
        assert record is not None
        assert len(record.documents) == 1

    def test_rerun_with_fewer_documents_drops_stale_rows(
        self, settings, table
    ) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [
                ExtractedDocument(index=0, fields=[]),
                ExtractedDocument(index=1, fields=[]),
                ExtractedDocument(index=2, fields=[]),
            ],
        )
        assert (PARTITION, "doc-002") in table.entities

        save_documents(
            settings,
            PARTITION,
            BLOB,
            [ExtractedDocument(index=0, fields=[])])

        assert (PARTITION, "doc-001") not in table.entities
        assert (PARTITION, "doc-002") not in table.entities
        record = load_record(settings, PARTITION)
        assert record is not None
        assert record.document_count == 1
        assert len(record.documents) == 1

    def test_a_legacy_meta_row_is_swept_up_as_stale(
        self, settings, table
    ) -> None:
        """A table written before the marker moved must not keep both rows.

        Leaving the old meta row behind would put the invoice-level columns in
        two places that could disagree, and the reader no longer looks at it.
        """
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [ExtractedDocument(index=0, fields=[])])
        table.entities[(PARTITION, "meta")] = {
            "PartitionKey": PARTITION,
            "RowKey": "meta",
            "invoiceId": PARTITION,
            "blobName": "an-old-photo.jpg",
        }

        save_documents(
            settings,
            PARTITION,
            BLOB,
            [ExtractedDocument(index=0, fields=[])])

        assert (PARTITION, "meta") not in table.entities
        assert (PARTITION, "doc-000") in table.entities

    def test_fields_and_the_finished_marker_are_written_together(
        self, settings, table
    ) -> None:
        """A reader must not see fields without the marker, or the reverse.

        Table Storage has no cross-entity transaction, so batching is the only
        way to avoid a window where the fields exist but nothing says the
        analysis finished. One submit_transaction covering both is what closes it.
        """
        table.submitted.clear()

        save_documents(
            settings,
            PARTITION,
            BLOB,
            [ExtractedDocument(index=0, fields=[])])

        assert len(table.submitted) == 1, "expected a single transaction"
        keys = {entity["RowKey"] for _, entity in table.submitted[0]}
        assert keys == {"doc-000"}
        _, marker = table.submitted[0][0]
        assert marker["documentCount"] == 1

    def test_the_marker_records_what_was_read(self, settings) -> None:
        save_documents(
            settings,
            PARTITION,
            BLOB,
            [ExtractedDocument(index=0, fields=[])])

        record = load_record(settings, PARTITION)
        assert record is not None
        assert record.invoice_id == PARTITION
        assert record.blob_name == BLOB
        assert record.created_at
        assert record.completed_at is not None


def test_unconfigured_service_reports_not_configured() -> None:
    assert not Settings().is_configured
