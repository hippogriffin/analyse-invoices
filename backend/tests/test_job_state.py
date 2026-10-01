"""The in-process job store and the overlay it feeds."""

from __future__ import annotations

import pytest

from app.config import JobStatus
from app.job_state import JobStateStore, get_job_state, overlay

PARTITION = "abc123"

STORED_DOCUMENTS = [
    {
        "index": 0,
        "confidence": None,
        "fields": {
            "VendorName": {"value": "Acme", "confidence": None},
            "InvoiceTotal": {"value": "120.00 EUR", "confidence": None},
        },
    }
]

LIVE_DOCUMENTS = [
    {
        "index": 0,
        "confidence": 0.98,
        "fields": {
            "VendorName": {"value": "Acme", "confidence": 0.99},
            "InvoiceTotal": {"value": "120.00 EUR", "confidence": 0.42},
        },
    }
]


class TestBounding:
    def test_stores_and_returns_a_result(self) -> None:
        store = JobStateStore()
        store.complete(PARTITION, LIVE_DOCUMENTS)
        held = store.get(PARTITION)
        assert held is not None
        assert held.status is JobStatus.COMPLETE
        assert held.documents == LIVE_DOCUMENTS

    def test_unknown_partition_returns_none(self) -> None:
        assert JobStateStore().get("nope") is None

    def test_oldest_entry_is_evicted_past_the_limit(self) -> None:
        store = JobStateStore(max_entries=2)
        store.begin("a")
        store.begin("b")
        store.begin("c")
        assert store.get("a") is None
        assert store.get("b") is not None
        assert store.get("c") is not None

    def test_re_touching_moves_an_entry_to_the_end(self) -> None:
        store = JobStateStore(max_entries=2)
        store.begin("a")
        store.begin("b")
        store.set_satisfied("a", True)
        store.begin("c")
        assert store.get("a") is not None, "recently used entry was evicted"
        assert store.get("b") is None

    def test_entries_expire(self) -> None:
        now = [0.0]
        store = JobStateStore(ttl_seconds=10, clock=lambda: now[0])
        store.complete(PARTITION, LIVE_DOCUMENTS)
        now[0] = 11.0
        assert store.get(PARTITION) is None

    def test_a_stored_result_is_copied(self) -> None:
        """A later mutation of the caller's dicts must not rewrite history."""
        store = JobStateStore()
        documents = [{"index": 0, "confidence": 0.5}]
        store.complete(PARTITION, documents)
        documents[0]["confidence"] = 0.1
        assert store.get(PARTITION).documents[0]["confidence"] == 0.5

    def test_a_returned_result_is_copied(self) -> None:
        """Otherwise a caller could rewrite the store through the return value."""
        store = JobStateStore()
        store.complete(PARTITION, LIVE_DOCUMENTS)
        store.get(PARTITION).documents[0]["confidence"] = 0.1
        assert store.get(PARTITION).documents[0]["confidence"] == 0.98

    def test_forget_drops_an_entry(self) -> None:
        store = JobStateStore()
        store.complete(PARTITION, LIVE_DOCUMENTS)
        store.forget(PARTITION)
        assert store.get(PARTITION) is None


class TestClaim:
    """Claiming is what stops one photo being analysed twice.

    Document Intelligence bills per page, so a lost race here is money, and the
    check-and-claim has to be one step to be worth anything.
    """

    def test_the_first_caller_wins(self) -> None:
        store = JobStateStore()
        assert store.claim(PARTITION) is True
        assert store.claim(PARTITION) is False

    def test_a_processing_job_is_still_claimed(self) -> None:
        store = JobStateStore()
        store.claim(PARTITION)
        store.processing(PARTITION)
        assert store.claim(PARTITION) is False, (
            "a running analysis must not be started a second time"
        )

    def test_a_failed_job_can_be_claimed_again(self) -> None:
        """A failed photo left nothing stored, so a retry is a new attempt."""
        store = JobStateStore()
        store.fail(PARTITION, "too blurry")
        assert store.claim(PARTITION) is True
        assert store.get(PARTITION).status is JobStatus.PENDING

    def test_a_completed_job_can_be_claimed_again(self) -> None:
        """The table is the authority on a finished analysis, not this store."""
        store = JobStateStore()
        store.complete(PARTITION, LIVE_DOCUMENTS)
        assert store.claim(PARTITION) is True

    def test_claiming_drops_what_a_previous_attempt_held(self) -> None:
        store = JobStateStore()
        store.fail(PARTITION, "boom")
        store.set_satisfied(PARTITION, False)
        store.claim(PARTITION)
        held = store.get(PARTITION)
        assert held.error is None
        assert held.satisfied is None

    def test_an_expired_entry_can_be_claimed(self) -> None:
        now = [0.0]
        store = JobStateStore(ttl_seconds=10, clock=lambda: now[0])
        store.claim(PARTITION)
        now[0] = 11.0
        assert store.claim(PARTITION) is True

    def test_concurrent_claims_produce_exactly_one_winner(self) -> None:
        import threading

        store = JobStateStore()
        winners: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(8, timeout=5)

        def claim() -> None:
            start.wait()
            won = store.claim(PARTITION)
            with lock:
                winners.append(won)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert winners.count(True) == 1, f"{winners.count(True)} callers claimed it"


class TestJobLifecycle:
    """The state the table no longer holds."""

    def test_a_started_job_is_pending(self) -> None:
        store = JobStateStore()
        store.begin(PARTITION)
        held = store.get(PARTITION)
        assert held.status is JobStatus.PENDING
        assert held.error is None
        assert held.satisfied is None

    def test_a_failure_carries_its_reason(self) -> None:
        store = JobStateStore()
        store.fail(PARTITION, "The photo was too blurry to read.")
        held = store.get(PARTITION)
        assert held.status is JobStatus.FAILED
        assert held.error == "The photo was too blurry to read."

    def test_a_failure_holds_no_documents(self) -> None:
        """A failed job has nothing to show, so nothing is kept for it."""
        store = JobStateStore()
        store.fail(PARTITION, "boom")
        assert store.get(PARTITION).documents is None

    def test_a_new_attempt_does_not_inherit_the_last_one(self) -> None:
        """A retry must not report the previous failure as its own."""
        store = JobStateStore()
        store.fail(PARTITION, "boom")
        store.begin(PARTITION)
        held = store.get(PARTITION)
        assert held.status is JobStatus.PENDING
        assert held.error is None
        assert held.satisfied is None

    def test_a_verdict_keeps_the_result_it_was_given_for(self) -> None:
        """Answering about the photo must not discard the fields beside it."""
        store = JobStateStore()
        store.complete(PARTITION, LIVE_DOCUMENTS)
        store.set_satisfied(PARTITION, False)
        held = store.get(PARTITION)
        assert held.satisfied is False
        assert held.status is JobStatus.COMPLETE
        assert held.documents == LIVE_DOCUMENTS

    def test_a_verdict_on_nothing_held_is_ignored(self) -> None:
        """There is no record to amend, and inventing one would be a lie."""
        store = JobStateStore()
        store.set_satisfied(PARTITION, True)
        assert store.get(PARTITION) is None


class TestOverlay:
    def test_confidence_is_filled_in_from_the_live_result(self) -> None:
        get_job_state().complete(PARTITION, LIVE_DOCUMENTS)
        merged = overlay(PARTITION, STORED_DOCUMENTS)
        assert merged[0]["confidence"] == 0.98
        assert merged[0]["fields"]["VendorName"]["confidence"] == 0.99
        assert merged[0]["fields"]["InvoiceTotal"]["confidence"] == 0.42

    def test_values_come_from_the_table_not_the_live_result(self) -> None:
        """The stored value wins; only the score is overlaid."""
        get_job_state().complete(PARTITION, LIVE_DOCUMENTS)
        stored = [
            {
                "index": 0,
                "docType": "invoice",
                "confidence": None,
                "fields": {"VendorName": {"value": "Corrected", "confidence": None}},
            }
        ]
        merged = overlay(PARTITION, stored)
        assert merged[0]["fields"]["VendorName"]["value"] == "Corrected"
        assert merged[0]["fields"]["VendorName"]["confidence"] == 0.99

    def test_nothing_is_invented_when_no_live_result_is_held(self) -> None:
        merged = overlay(PARTITION, STORED_DOCUMENTS)
        assert merged[0]["confidence"] is None
        assert all(
            field["confidence"] is None
            for field in merged[0]["fields"].values()
        )

    def test_a_field_absent_from_the_live_result_keeps_none(self) -> None:
        get_job_state().complete(
            PARTITION,
            [
                {
                    "index": 0,
                    "confidence": 0.9,
                    "fields": {"VendorName": {"value": "Acme", "confidence": 0.9}},
                }
            ],
        )
        merged = overlay(PARTITION, STORED_DOCUMENTS)
        assert merged[0]["fields"]["VendorName"]["confidence"] == 0.9
        assert merged[0]["fields"]["InvoiceTotal"]["confidence"] is None

    def test_a_stale_document_is_left_alone(self) -> None:
        """A document the table has but memory does not is not zeroed or guessed."""
        get_job_state().complete(
            PARTITION,
            [{"index": 0, "confidence": 0.9, "fields": {}}],
        )
        stored = [
            {"index": 0, "confidence": None, "fields": {}},
            {"index": 1, "confidence": None, "fields": {}},
        ]
        merged = overlay(PARTITION, stored)
        assert merged[0]["confidence"] == 0.9
        assert merged[1]["confidence"] is None

    def test_the_input_is_not_mutated(self) -> None:
        get_job_state().complete(PARTITION, LIVE_DOCUMENTS)
        overlay(PARTITION, STORED_DOCUMENTS)
        assert STORED_DOCUMENTS[0]["confidence"] is None
        assert (
            STORED_DOCUMENTS[0]["fields"]["VendorName"]["confidence"] is None
        )

    def test_a_field_the_table_lost_is_not_reintroduced(self) -> None:
        """Only fields present in the table come back, whatever memory holds."""
        get_job_state().complete(PARTITION, LIVE_DOCUMENTS)
        merged = overlay(PARTITION, [STORED_DOCUMENTS[0]])
        assert set(merged[0]["fields"]) == {"VendorName", "InvoiceTotal"}
