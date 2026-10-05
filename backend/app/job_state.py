"""Per-invoice state that is deliberately not written to Table Storage.

The table holds one thing only: the result of an analysis that succeeded. A photo
that was never read, or that failed to read, leaves no trace there, and neither
does anything said about the photo afterwards. That leaves a gap the status
endpoint cannot fill from storage alone, because the interesting answers are
precisely the ones that were not stored:

* how the job is going, before it finishes
* why it failed, when it does
* the confidence attached to each reading
* whether the reviewer was happy with the photo

This module holds those answers in the process that produced them, and the status
endpoint overlays them on the stored values. One store rather than several,
because they share a key, a lifetime and an eviction policy.

The consequences are deliberate rather than hidden:

* A row in the table means the analysis succeeded, and nothing else does. The
  presence of a row is what makes an invoice ``complete``.
* After a restart, a completed invoice still reports its values, but with no
  confidence, no failure reason and no verdict. Nothing is guessed: the UI omits
  a badge rather than inventing a score, and asks about the photo again.
* A job that was running when the process stopped is simply unknown afterwards,
  so the status endpoint answers 404. The source photo is deleted either way, so
  a job that has lost its state is not worth resuming.
* The store is bounded by entry count and age, so a long-lived process cannot
  grow without limit. A restart is a valid way to drop it.

Entries are keyed by invoice partition and hold response-shaped document dicts
rather than repository types, which keeps this module free of any import from
``app.repository`` and avoids a cycle.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import JobStatus

# Enough for a reviewer to work through a queue of invoices without evicting the
# one in front of them; small, because each entry can hold a whole result.
MAX_ENTRIES = 64

# A result nobody is looking at any more is not worth holding. This is longer
# than a client is expected to poll for, so a slow reader is not cut off.
TTL_SECONDS = 30 * 60

Clock = Callable[[], float]


@dataclass
class JobState:
    """What is known about one invoice without consulting storage."""

    status: JobStatus
    error: str | None = None
    #: Response-shaped documents, present only for a completed analysis. They
    #: carry the confidence that the table deliberately omits.
    documents: list[dict[str, Any]] | None = None
    #: None until the reviewer answers whether the photo was good enough, then
    #: True to accept it or False to retake. The UI prompts on None.
    satisfied: bool | None = None
    #: The blob the analysis read, kept so the record can be written when the
    #: reviewer confirms. The photo itself is already gone by then; this names it
    #: for the audit trail on the marker row.
    blob_name: str = ""


@dataclass
class _Entry:
    stored_at: float
    state: JobState
    documents: list[dict[str, Any]] = field(default_factory=list)


class JobStateStore:
    """A bounded, expiring map of invoice id to its unstored job state."""

    def __init__(
        self,
        max_entries: int = MAX_ENTRIES,
        ttl_seconds: float = TTL_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()

    def begin(self, partition: str) -> None:
        """Record that a job was accepted, replacing any earlier attempt."""
        self._store(partition, JobState(status=JobStatus.PENDING))

    def claim(self, partition: str) -> bool:
        """Reserve an invoice for analysis, or report that it is already claimed.

        This is the whole of the dedupe decision, in one atomic step. Doing it as
        a check followed by a separate ``begin`` leaves a window between the two:
        the caller has to await the blob write in between, and a second request
        for the same photo arriving in that window sees nothing claimed and
        queues a second analysis. Document Intelligence bills per page, so that
        window is a real cost rather than a cosmetic one.

        Returns True when this call took the claim, False when the invoice is
        already pending or processing. A failed or absent claim is not a refusal
        to try again later: a resubmission of a failed photo is a new attempt, so
        only an in-flight job blocks.
        """
        with self._lock:
            self._evict_locked()
            entry = self._entries.get(partition)
            if entry is not None and entry.state.status in (
                JobStatus.PENDING,
                JobStatus.PROCESSING,
            ):
                return False
            self._entries[partition] = _Entry(
                stored_at=self._clock(),
                state=JobState(status=JobStatus.PENDING),
                documents=[],
            )
            self._entries.move_to_end(partition)
            self._evict_locked()
            return True

    def processing(self, partition: str) -> None:
        self._update(partition, lambda state: setattr(state, "status", JobStatus.PROCESSING))

    def complete(
        self,
        partition: str,
        documents: Sequence[dict[str, Any]],
        blob_name: str = "",
    ) -> None:
        """Remember a result. Documents are copied, so later mutation is safe."""
        self._store(
            partition,
            JobState(status=JobStatus.COMPLETE, blob_name=blob_name),
            documents=[dict(document) for document in documents],
        )

    def fail(self, partition: str, error: str) -> None:
        self._store(partition, JobState(status=JobStatus.FAILED, error=error))

    def set_satisfied(self, partition: str, satisfied: bool) -> None:
        """Record the reviewer's verdict on the photo, leaving the rest alone."""
        self._update(partition, lambda state: setattr(state, "satisfied", satisfied))

    def get(self, partition: str) -> JobState | None:
        """The held state for an invoice, or ``None`` if it is not held.

        Reading refreshes the entry's age. The TTL exists so a result nobody is
        looking at any more is not held for ever, and a client polling the status
        is looking: without this the entry expires on schedule regardless of the
        attention it is getting. That matters more than it used to, because the
        record is only written when the reviewer accepts, so an expiry part-way
        through a long review takes away the values they are judging.
        """
        with self._lock:
            self._evict_locked()
            entry = self._entries.get(partition)
            if entry is None:
                return None
            entry.stored_at = self._clock()
            self._entries.move_to_end(partition)
            return JobState(
                status=entry.state.status,
                error=entry.state.error,
                documents=[dict(document) for document in entry.documents] or None,
                satisfied=entry.state.satisfied,
                blob_name=entry.state.blob_name,
            )

    def forget(self, partition: str) -> None:
        """Drop held state, so a resubmission is treated as a new attempt."""
        with self._lock:
            self._entries.pop(partition, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _store(
        self,
        partition: str,
        state: JobState,
        documents: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            self._entries[partition] = _Entry(
                stored_at=self._clock(),
                state=state,
                documents=[dict(document) for document in documents or []],
            )
            self._entries.move_to_end(partition)
            self._evict_locked()

    def _update(self, partition: str, change: Callable[[JobState], None]) -> None:
        """Apply a change to held state, keeping what is already known.

        A verdict or a status change must not drop the result that was stored
        alongside it, so the existing documents are carried over. If nothing is
        held, there is nothing to amend and the change is ignored.
        """
        with self._lock:
            self._evict_locked()
            entry = self._entries.get(partition)
            if entry is None:
                return
            change(entry.state)
            entry.stored_at = self._clock()
            self._entries.move_to_end(partition)

    def _evict_locked(self) -> None:
        now = self._clock()
        stale = [
            key
            for key, entry in self._entries.items()
            if now - entry.stored_at > self._ttl_seconds
        ]
        for key in stale:
            del self._entries[key]
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


_STORE = JobStateStore()


def get_job_state() -> JobStateStore:
    """The process-wide store. One process serves one set of results."""
    return _STORE


def overlay(
    partition: str, documents: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay held confidence onto stored document dicts.

    The values come from the table and the scores from memory, so they are
    merged per document index and per field name. A document or field with no
    held result keeps ``confidence: None``; nothing is ever guessed.

    Documents in ``documents`` are not modified, so the caller can keep the
    stored version intact.
    """
    held = get_job_state().get(partition)
    scores: dict[int, dict[str, Any]] = {}
    if held and held.documents:
        for document in held.documents:
            if isinstance(document.get("index"), int):
                scores[document["index"]] = document

    merged: list[dict[str, Any]] = []
    for document in documents:
        document = dict(document)
        source = scores.get(document.get("index"))
        if source is not None:
            document["confidence"] = source.get("confidence")
            held_fields = source.get("fields") or {}
            fields = {}
            for name, field in (document.get("fields") or {}).items():
                field = dict(field)
                field_confidence = (held_fields.get(name) or {}).get("confidence")
                if field_confidence is not None:
                    field["confidence"] = field_confidence
                fields[name] = field
            document["fields"] = fields
        merged.append(document)
    return merged
