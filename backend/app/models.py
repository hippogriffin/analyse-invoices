"""Request and response models for the analysis API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InvoiceAccepted(BaseModel):
    """Response to a successful submission; analysis continues in the background."""

    model_config = ConfigDict(populate_by_name=True)

    invoice_id: str = Field(alias="invoiceId")
    status: Literal["pending"] = "pending"
    status_url: str = Field(alias="statusUrl")
    #: True when the photo is already stored, or is already being analysed, so
    #: there is no new analysis and no result to wait for. Document Intelligence
    #: bills per page, so the repeat is deliberately free, and the UI says the
    #: photo is already processed rather than making the reviewer wait for
    #: something that will never arrive.
    #:
    #: A result that has been read but not yet accepted is *not* reported this
    #: way: nothing is stored yet, and its values are still waiting to be
    #: reviewed, so the caller should go and fetch them.
    already_processed: bool = Field(default=False, alias="alreadyProcessed")


class DocumentFieldOut(BaseModel):
    value: Any = None
    confidence: float | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    #: Page order, recovered from the row key. Not a stored column.
    index: int
    confidence: float | None = None
    fields: dict[str, DocumentFieldOut] = Field(default_factory=dict)


class InvoiceStatusOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    invoice_id: str = Field(alias="invoiceId")
    #: Names the blob that was read. The photo itself is deleted once processing
    #: finishes, so this is a record of what was analysed, not a live reference.
    blob_name: str = Field(alias="blobName")
    #: Served from the in-process job state, not the table: a row exists only for
    #: a finished analysis, so a completed record always reports "complete".
    status: Literal["pending", "processing", "complete", "failed"]
    created_at: str = Field(default="", alias="createdAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    #: Also transient, and absent after a restart, so the UI reports the failure
    #: while the process lives and says only that it lost track afterwards.
    error: str | None = None
    document_count: int = Field(default=0, alias="documentCount")
    #: None until the reviewer says whether the photo was good enough, then True
    #: to accept it or False to retake. The UI prompts on None, so the three
    #: states are distinguishable.
    photo_satisfied: bool | None = Field(default=None, alias="photoSatisfied")
    documents: list[DocumentOut] = Field(default_factory=list)
    #: The month's allowance, how much of it has been spent and how much is left.
    #: Every analysis run spends one, whether or not the result is kept, so the
    #: numbers are the same for a photo that is later rejected. They are absent
    #: only when the usage table could not be read, rather than guessed at. This
    #: is reported, not enforced: the service still analyses whatever it is sent.
    monthly_allowance: int | None = Field(default=None, alias="monthlyAllowance")
    documents_used: int | None = Field(default=None, alias="documentsUsed")
    documents_remaining: int | None = Field(default=None, alias="documentsRemaining")


class DocumentFieldPatch(BaseModel):
    """A reviewer-corrected value for one field.

    Document Intelligence reads some fields wrong and misses others, so the
    reviewer is given a chance to fix the reading before it is stored. Only the
    value matters: the confidence of a reading that has been overwritten by a
    person is meaningless, so it is not carried here.
    """

    value: Any = None


class DocumentPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    #: Page order, matching the index of the document being corrected.
    index: int
    fields: dict[str, DocumentFieldPatch] = Field(default_factory=dict)


class InvoiceCorrections(BaseModel):
    """Values the reviewer entered before accepting a reading.

    Sent on ``POST /api/invoices/{id}/photo`` when the reviewer changed or added
    fields. The backend merges them over the extracted reading before the record
    is written, so what is stored is what the reviewer saw and approved. A
    missing document keeps the extracted values; a field not named keeps its
    extracted value.
    """

    documents: list[DocumentPatch] = Field(default_factory=list)


class ServiceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model: str
    source_container: str = Field(alias="sourceContainer")
    result_table: str = Field(alias="resultTable")
    # Published so a UI can reject a bad photo before uploading it. This service
    # still enforces both, so a client that ignores them gains nothing.
    max_upload_bytes: int = Field(alias="maxUploadBytes")
    allowed_content_types: tuple[str, ...] = Field(alias="allowedContentTypes")
