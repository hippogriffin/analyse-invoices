"""HTTP contract for the analysis API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import main as main_module
from app.analyzer import AnalysisError
from app.config import JobStatus
from app.job_state import get_job_state
from app.repository import (
    ExtractedDocument,
    ExtractedField,
    dedupe_key,
    load_record,
)
from app.storage import PhotoStorageError

PHOTO = b"\xff\xd8\xff-not-a-real-jpeg"
OTHER_PHOTO = b"\x89PNG\r\n\x1a\n-a-different-photo"
PARTITION = dedupe_key(PHOTO)
BLOB = f"2026/{PARTITION}.jpg"


def post_photo(client, photo: bytes = PHOTO, name: str = "invoice.jpg", type: str = "image/jpeg"):
    return client.post("/api/invoices", files={"photo": (name, photo, type)})


@pytest.fixture
def stub_azure(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the Azure calls that the API makes around the background task."""
    state: dict[str, object] = {
        "documents": [
            ExtractedDocument(
                index=0,
                confidence=0.98,
                fields=[
                    ExtractedField("VendorName", "Acme", 0.99),
                    ExtractedField("InvoiceTotal", "120.00 EUR", 0.95),
                ],
            )
        ],
        "error": None,
        "runs": 0,
        "stored": [],
        "stored_names": [],
    }

    def fake_download(_settings, _blob_name: str) -> bytes:
        return PHOTO

    def fake_analyze(_settings, photo: bytes) -> list[ExtractedDocument]:
        state["runs"] = int(state["runs"]) + 1
        if state["error"]:
            raise AnalysisError(str(state["error"]))
        return state["documents"]  # type: ignore[return-value]

    real_store = main_module.store_photo

    def spy_store(data: bytes, content_type: str, settings):
        """Record the write, then really perform it.

        Replacing the store instead of wrapping it would leave the fake container
        empty, and every assertion that the photo was deleted would then pass
        without anything having been deleted.
        """
        stored = real_store(data, content_type, settings)
        state["stored"].append(data)  # type: ignore[union-attr]
        state["stored_names"].append(stored.name)  # type: ignore[union-attr]
        return stored

    monkeypatch.setattr(main_module, "download_photo", fake_download)
    monkeypatch.setattr(main_module, "analyze", fake_analyze)
    monkeypatch.setattr(main_module, "store_photo", spy_store)
    return state


def test_startup_creates_the_results_table(client, table_service) -> None:
    """Regression: ensure_table must go through the service client.

    TableClient has no create_table_if_not_exists method, and the failure was
    swallowed, so a missing table silently looked like a successful startup.
    """
    assert table_service.created_tables == ["invoices"]


def test_ensure_table_logs_instead_of_raising(
    settings, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from app import clients as clients_module

    def boom(*_a, **_k):
        raise RuntimeError("no permission")

    monkeypatch.setattr(clients_module, "table_service", boom)
    with caplog.at_level("WARNING"):
        clients_module.ensure_table(settings)
    assert "invoices" in caplog.text


def test_healthz(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_exposes_non_secret_settings(client) -> None:
    body = client.get("/api/config").json()
    assert body["model"] == "prebuilt-invoice"
    assert body["sourceContainer"] == "pictures"
    assert body["resultTable"] == "invoices"
    # A stranded-job threshold would mean nothing now that no status is stored.
    assert "staleJobAfterSeconds" not in body


def test_config_publishes_the_upload_limits(client) -> None:
    """A UI needs the limits to reject a bad photo before sending it."""
    body = client.get("/api/config").json()
    assert body["maxUploadBytes"] == 10 * 1024 * 1024
    assert "image/jpeg" in body["allowedContentTypes"]


def test_submit_returns_202_with_an_id_and_runs_analysis(client, stub_azure) -> None:
    response = post_photo(client)
    assert response.status_code == 202
    body = response.json()
    assert body["invoiceId"] == PARTITION
    assert body["status"] == "pending"
    assert body["statusUrl"] == f"/api/invoices/{PARTITION}"
    assert stub_azure["runs"] == 1


def test_the_photo_is_read_from_storage(client, stub_azure) -> None:
    """The service owns storage, so it must read back the bytes it was sent."""
    post_photo(client, name="march.png", type="image/png", photo=OTHER_PHOTO)
    assert stub_azure["stored"] == [OTHER_PHOTO]


def test_blob_name_is_derived_from_the_content(client, stub_azure) -> None:
    """The caller never names a blob, so it cannot point at another one."""
    post_photo(client)
    assert stub_azure["stored_names"] == [BLOB]


def test_status_returns_extracted_fields(client, stub_azure) -> None:
    post_photo(client)
    body = client.get(f"/api/invoices/{PARTITION}").json()
    assert body["status"] == "complete"
    assert body["blobName"] == BLOB
    assert body["documentCount"] == 1
    assert body["error"] is None
    document = body["documents"][0]
    assert document["index"] == 0
    assert document["fields"]["VendorName"]["value"] == "Acme"
    assert document["fields"]["InvoiceTotal"]["confidence"] == 0.95


def test_unknown_invoice_id_is_404(client) -> None:
    response = client.get("/api/invoices/does-not-exist")
    assert response.status_code == 404
    assert "Unknown invoice id" in response.json()["detail"]


def test_identical_photo_reuses_the_same_id(client, stub_azure) -> None:
    first = post_photo(client).json()
    second = post_photo(client).json()
    assert first["invoiceId"] == second["invoiceId"] == PARTITION


def test_identical_photo_is_not_analysed_twice(client, stub_azure) -> None:
    """Document Intelligence bills per page, so a repeat must be free."""
    post_photo(client)
    post_photo(client)
    assert stub_azure["runs"] == 1


def test_a_fresh_photo_is_not_reported_as_already_processed(
    client, stub_azure
) -> None:
    body = post_photo(client).json()
    assert body["alreadyProcessed"] is False, (
        "a new photo must be analysed, and the UI must not claim otherwise"
    )


class TestAPhotoVerdict:
    """The verdict is what decides whether anything is kept at all.

    The values are read, shown and then disputed. Until the reviewer says the
    photo was readable there is nothing worth storing, so the table stays empty;
    saying the photo was bad takes the result away rather than recording an
    opinion about it.
    """

    def test_the_question_is_open_before_an_answer(self, client, stub_azure) -> None:
        post_photo(client)
        assert client.get(f"/api/invoices/{PARTITION}").json()["photoSatisfied"] is None

    def test_nothing_is_stored_before_the_reading_is_accepted(
        self, client, stub_azure, table
    ) -> None:
        """The read values are on screen, but unreviewed, so they are not kept."""
        post_photo(client)
        assert table.entities == {}

    def test_the_values_are_available_to_review_without_being_stored(
        self, client, stub_azure
    ) -> None:
        """Something has to be shown to be judged, and it is held in process."""
        post_photo(client)
        body = client.get(f"/api/invoices/{PARTITION}").json()
        assert body["status"] == "complete"
        assert body["documents"][0]["fields"]["VendorName"]["value"] == "Acme"
        assert body["documentCount"] == 1

    def test_answering_yes_stores_the_record(self, client, stub_azure, table) -> None:
        post_photo(client)
        body = client.post(f"/api/invoices/{PARTITION}/photo").json()
        assert body["photoSatisfied"] is True
        assert client.get(f"/api/invoices/{PARTITION}").json()["photoSatisfied"] is True
        assert table.entities[(PARTITION, "doc-000")]["f_VendorName"] == "Acme"

    def test_accepting_twice_writes_once(
        self, client, stub_azure, monkeypatch
    ) -> None:
        """A repeated confirmation is a no-op, not a second billable write."""
        post_photo(client)
        client.post(f"/api/invoices/{PARTITION}/photo")
        calls: list[object] = []
        real_save = main_module.save_documents

        def counting_save(*args, **kwargs):
            calls.append(args)
            return real_save(*args, **kwargs)

        monkeypatch.setattr(main_module, "save_documents", counting_save)
        assert client.post(f"/api/invoices/{PARTITION}/photo").status_code == 200
        assert calls == []

    def test_a_storage_failure_is_not_reported_as_a_bad_photo(
        self, client, stub_azure, monkeypatch
    ) -> None:
        """The photo was fine; the write was not. 502 says which one failed."""
        from app.clients import StorageWriteError

        post_photo(client)

        def boom(*_args, **_kwargs):
            raise StorageWriteError("table unavailable")

        monkeypatch.setattr(main_module, "save_documents", boom)
        response = client.post(f"/api/invoices/{PARTITION}/photo")
        assert response.status_code == 502
        assert "table unavailable" in response.json()["detail"]
        # The result is still held, so the reviewer can accept again once the
        # table is back rather than paying for a second analysis.
        assert client.get(f"/api/invoices/{PARTITION}").json()["documents"]

    def test_a_failed_analysis_cannot_be_accepted(
        self, client, stub_azure
    ) -> None:
        """There is nothing to accept, and saying so beats storing a blank row."""
        stub_azure["error"] = "The photo is too blurry."
        post_photo(client, photo=OTHER_PHOTO)
        response = client.post(f"/api/invoices/{dedupe_key(OTHER_PHOTO)}/photo")
        assert response.status_code == 409
        assert "accept" in response.json()["detail"]

    def test_asking_for_a_retake_keeps_nothing(
        self, client, stub_azure, table
    ) -> None:
        post_photo(client)
        response = client.delete(f"/api/invoices/{PARTITION}/photo")
        assert response.status_code == 204
        assert response.content == b""
        assert table.entities == {}
        assert client.get(f"/api/invoices/{PARTITION}").status_code == 404

    def test_a_stored_record_is_withdrawn_by_a_later_retake(
        self, client, stub_azure, table
    ) -> None:
        """A reviewer who changes their mind is not an error.

        The verdict is transient, so after a restart a stored record is on screen
        with no memory of having been accepted. Rejecting the photo then has to
        remove what is on screen, not merely record that it was doubted.
        """
        post_photo(client)
        client.post(f"/api/invoices/{PARTITION}/photo")
        assert table.entities
        client.delete(f"/api/invoices/{PARTITION}/photo")
        assert table.entities == {}
        assert client.get(f"/api/invoices/{PARTITION}").status_code == 404

    def test_the_verdict_itself_is_not_stored(
        self, client, stub_azure, table
    ) -> None:
        """A record is the values and the proof they were accepted. Nothing more."""
        post_photo(client)
        client.post(f"/api/invoices/{PARTITION}/photo")
        stored = {
            name
            for entity in table.entities.values()
            for name in entity
            if not name.startswith("odata.")
        }
        assert not [name for name in stored if "atisf" in name], sorted(stored)

    def test_an_unknown_invoice_is_rejected(self, client) -> None:
        assert client.post("/api/invoices/nope/photo").status_code == 404
        assert client.delete("/api/invoices/nope/photo").status_code == 404

    def test_a_verdict_touches_no_blob_storage(self, client, stub_azure, monkeypatch) -> None:
        """Neither answer needs the photo: it is already gone by this point."""
        def boom(*_args, **_kwargs):
            raise AssertionError("the verdict endpoints must not touch blob storage")

        # Patched after the analysis has run, so it guards the verdict calls and
        # not the cleanup the analysis legitimately performs.
        post_photo(client)
        monkeypatch.setattr(main_module, "delete_photo", boom)
        assert client.post(f"/api/invoices/{PARTITION}/photo").status_code == 200
        assert client.delete(f"/api/invoices/{PARTITION}/photo").status_code == 204


def test_a_repeated_photo_says_so(client, stub_azure) -> None:
    """The UI needs to tell "already stored" from "being read right now".

    Without this the second submission looks like a fresh one, so the reviewer
    waits for an analysis that was deliberately not run.
    """
    post_photo(client)
    client.post(f"/api/invoices/{PARTITION}/photo")
    body = post_photo(client).json()
    assert body["alreadyProcessed"] is True
    assert body["invoiceId"] == PARTITION


def test_a_repeat_of_an_unaccepted_photo_is_not_analysed_again(
    client, stub_azure
) -> None:
    """Same photo, still unconfirmed: the values are already held, so re-read it
    for nothing but hand the reviewer back to the answer they have not given.

    Reported as fresh, because there is something still waiting to be accepted:
    calling it already processed would strand the values with no way to accept
    them. Document Intelligence is not billed again either way.
    """
    post_photo(client)
    body = post_photo(client).json()
    assert int(stub_azure["runs"]) == 1, "the repeat was analysed again"
    assert body["alreadyProcessed"] is False
    assert body["invoiceId"] == PARTITION
    # The status endpoint hands the held result straight back, so the reviewer
    # lands on the values rather than on a new wait.
    assert client.get(f"/api/invoices/{PARTITION}").json()["documents"]


def test_a_photo_being_analysed_says_so(client) -> None:
    """A repeat while the first is still running is the same answer to the user."""
    get_job_state().begin(PARTITION)
    body = post_photo(client).json()
    assert body["alreadyProcessed"] is True
    assert body["status"] == "pending"


def test_a_failed_photo_is_analysed_again_not_called_processed(
    client, stub_azure
) -> None:
    """A retry must not be reported as a repeat: nothing was stored for it."""
    stub_azure["error"] = "No invoice was recognised."
    post_photo(client)
    stub_azure["error"] = None
    body = post_photo(client).json()
    assert body["alreadyProcessed"] is False


def test_identical_photo_does_not_rewrite_the_blob(
    client, stub_azure, blob_container
) -> None:
    post_photo(client)
    first_writes = dict(blob_container.blobs)
    post_photo(client)
    assert blob_container.blobs == first_writes


def test_a_different_photo_is_a_different_invoice(client, stub_azure) -> None:
    first = post_photo(client).json()
    second = post_photo(
        client, photo=OTHER_PHOTO, name="other.png", type="image/png"
    ).json()
    assert first["invoiceId"] != second["invoiceId"]
    assert stub_azure["runs"] == 2


def test_a_failed_photo_can_be_submitted_again(client, stub_azure) -> None:
    """Dedupe must not make a bad photo permanent: a retry has to re-run."""
    stub_azure["error"] = "No invoice was recognised."
    post_photo(client)
    assert client.get(f"/api/invoices/{PARTITION}").json()["status"] == "failed"

    stub_azure["error"] = None
    retry = post_photo(client).json()
    assert retry["invoiceId"] == PARTITION
    assert stub_azure["runs"] == 2
    assert client.get(f"/api/invoices/{PARTITION}").json()["status"] == "complete"


def test_analysis_failure_is_recorded_not_raised(client, stub_azure) -> None:
    stub_azure["error"] = "No invoice was recognised."
    response = post_photo(client)
    # The submission is still accepted; the failure surfaces on the status read.
    assert response.status_code == 202
    body = client.get(f"/api/invoices/{PARTITION}").json()
    assert body["status"] == "failed"
    assert body["error"] == "No invoice was recognised."
    assert body["documents"] == []


def test_unexpected_background_error_is_contained(
    client, stub_azure, monkeypatch
) -> None:
    def explode(*_args: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main_module, "analyze", explode)
    post_photo(client)
    body = client.get(f"/api/invoices/{PARTITION}").json()
    assert body["status"] == "failed"
    assert "Retry the photo" in body["error"]


class TestSubmissionValidation:
    def test_missing_file_field_is_422(self, client) -> None:
        assert client.post("/api/invoices").status_code == 422

    def test_wrong_field_name_is_422(self, client) -> None:
        assert client.post("/api/invoices", files={"image": ("a.jpg", PHOTO, "image/jpeg")}).status_code == 422

    def test_unsupported_type_is_400(self, client, stub_azure) -> None:
        response = post_photo(client, photo=b"%PDF", name="notes.pdf", type="application/pdf")
        assert response.status_code == 400
        assert "Unsupported image type" in response.json()["detail"]
        assert stub_azure["runs"] == 0

    def test_type_with_parameters_is_accepted(self, client, stub_azure) -> None:
        """Some browsers append a charset to the part's content type."""
        response = post_photo(client, type="image/jpeg; charset=binary")
        assert response.status_code == 202

    def test_empty_file_is_400(self, client, stub_azure) -> None:
        assert post_photo(client, photo=b"").status_code == 400
        assert stub_azure["runs"] == 0

    def test_oversized_file_is_413(
        self, settings, stub_azure, monkeypatch
    ) -> None:
        """The limit is read from the app's settings, so build a fresh app."""
        from fastapi.testclient import TestClient

        from app.config import get_settings
        from app.main import create_app

        monkeypatch.setenv("MAX_UPLOAD_BYTES", "1024")
        get_settings.cache_clear()
        with TestClient(create_app()) as small:
            response = post_photo(small, photo=b"x" * 2048)
        assert response.status_code == 413
        assert stub_azure["runs"] == 0

    def test_storage_failure_is_502(self, client, stub_azure, monkeypatch) -> None:
        from app.storage import PhotoStorageError, store_photo

        def boom(*_a, **_k):
            raise PhotoStorageError("Blob storage rejected the credentials.")

        monkeypatch.setattr(main_module, "store_photo", boom)
        response = post_photo(client)
        assert response.status_code == 502
        assert "rejected the credentials" in response.json()["detail"]
        # Nothing was queued, so no invoice exists to poll.
        assert client.get(f"/api/invoices/{PARTITION}").status_code == 404


def test_unconfigured_service_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as unconfigured:
        response = unconfigured.post(
            "/api/invoices", files={"photo": ("a.jpg", PHOTO, "image/jpeg")}
        )
    assert response.status_code == 503
    assert "DOCUMENT_INTELLIGENCE_ENDPOINT" in response.json()["detail"]


def test_pending_state_is_visible_before_analysis(client) -> None:
    """A job with no row yet is still reportable, from the process state."""
    get_job_state().begin(PARTITION)
    body = client.get(f"/api/invoices/{PARTITION}").json()
    assert body["status"] == "pending"
    assert body["documents"] == []
    assert body["documentCount"] == 0


def test_processing_state_is_visible_while_running(client) -> None:
    get_job_state().begin(PARTITION)
    get_job_state().processing(PARTITION)
    body = client.get(f"/api/invoices/{PARTITION}").json()
    assert body["status"] == "processing"


def test_a_job_nobody_is_holding_is_unknown(client) -> None:
    """No row and no held state means the service cannot vouch for the invoice.

    This is what a restart mid-analysis looks like from outside: the status
    endpoint answers 404 rather than inventing a status for a job it has no
    memory of.
    """
    assert client.get(f"/api/invoices/{PARTITION}").status_code == 404


def test_a_stored_result_wins_over_a_stale_held_state(client, stub_azure) -> None:
    """A row means the reading was accepted, whatever the process remembers."""
    post_photo(client)
    client.post(f"/api/invoices/{PARTITION}/photo")
    get_job_state().begin(PARTITION)
    body = client.get(f"/api/invoices/{PARTITION}").json()
    assert body["status"] == "complete"
    assert body["documents"]
    # The record is proof of acceptance, so a stale pending state must not make
    # the reviewer answer a question that has already been answered.
    assert body["photoSatisfied"] is True


def test_an_unaccepted_result_does_not_outlive_the_process(
    client, stub_azure
) -> None:
    """The cost of storing nothing until the reviewer agrees.

    A result that has been read but not accepted exists only in this process, so
    a restart loses it: the values are gone, the status is unknown, and the
    reviewer has to send the photo again and pay to read it twice. That is the
    price of a table that never holds a reading nobody approved.
    """
    post_photo(client)
    get_job_state().clear()
    assert client.get(f"/api/invoices/{PARTITION}").status_code == 404


def test_cors_headers_are_applied(client, monkeypatch) -> None:
    response = client.get("/api/config", headers={"Origin": "https://cam.example"})
    assert response.headers.get("access-control-allow-origin") == "*"


def test_cors_allows_the_multipart_post_from_the_ui(client) -> None:
    """The UI is a separate origin, so the browser preflights the file POST."""
    response = client.options(
        "/api/invoices",
        headers={
            "Origin": "https://cam.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"


class TestConfidenceIsNotStoredButStillReturned:
    def test_response_carries_confidence(self, client, stub_azure) -> None:
        post_photo(client)
        body = client.get(f"/api/invoices/{PARTITION}").json()
        assert body["documents"][0]["confidence"] == 0.98
        assert body["documents"][0]["fields"]["VendorName"]["confidence"] == 0.99

    def test_table_holds_no_confidence(self, client, stub_azure, table) -> None:
        """The response and the storage disagree, on purpose.

        The scores come from the process, so a passing response must not be taken
        as evidence that anything was written.
        """
        post_photo(client)
        client.post(f"/api/invoices/{PARTITION}/photo")
        stored = {
            name
            for entity in table.entities.values()
            for name in entity
            if name.lower().startswith("c_") or name.lower() == "confidence"
        }
        assert stored == set()
        # The values are still there.
        assert table.entities[(PARTITION, "doc-000")]["f_VendorName"] == "Acme"

    def test_confidence_is_absent_once_the_process_forgets_it(
        self, client, stub_azure
    ) -> None:
        """A restart loses the scores; the values survive and nothing is faked."""
        post_photo(client)
        client.post(f"/api/invoices/{PARTITION}/photo")
        get_job_state().clear()

        body = client.get(f"/api/invoices/{PARTITION}").json()
        assert body["status"] == "complete"
        assert body["documents"][0]["fields"]["VendorName"]["value"] == "Acme"
        assert body["documents"][0]["confidence"] is None
        assert body["documents"][0]["fields"]["VendorName"]["confidence"] is None


class TestThePhotoIsDeletedOnceRead:
    """The photo is a means, not a record.

    These are the tests that would fail if the blob were left behind, which is the
    whole point: an image of someone's paperwork should not outlive the reading.
    """

    def test_a_successful_read_leaves_no_blob(
        self, client, stub_azure, blob_container
    ) -> None:
        post_photo(client)
        assert blob_container.blobs == {}, "the photo outlived the analysis"

    def test_a_failed_read_leaves_no_blob(
        self, client, stub_azure, blob_container
    ) -> None:
        """A failure is no reason to keep someone's document around."""
        stub_azure["error"] = "The photo was too blurry to read."
        post_photo(client)
        assert blob_container.blobs == {}

    def test_the_extracted_values_survive_the_deletion(
        self, client, stub_azure, blob_container
    ) -> None:
        post_photo(client)
        body = client.get(f"/api/invoices/{PARTITION}").json()
        assert blob_container.blobs == {}
        assert body["documents"][0]["fields"]["VendorName"]["value"] == "Acme"

    def test_a_deletion_failure_does_not_fail_a_good_read(
        self, client, stub_azure, blob_container, monkeypatch
    ) -> None:
        """A cleanup problem must not turn a good extraction into a failure.

        The fields are already written by this point. Reporting the analysis as
        failed because a delete call did not work would hide a usable result
        behind an error about a file the user cannot see anyway.
        """
        def boom(*_args, **_kwargs):
            raise PhotoStorageError("storage is unavailable")

        monkeypatch.setattr(main_module, "delete_photo", boom)
        post_photo(client)
        body = client.get(f"/api/invoices/{PARTITION}").json()
        assert body["status"] == "complete"
        assert body["documents"][0]["fields"]["VendorName"]["value"] == "Acme"
        assert BLOB in blob_container.blobs, "the delete was faked, so it is there"


class TestAResubmission:
    def test_a_failed_photo_can_be_submitted_again(self, client, stub_azure) -> None:
        """A failure leaves no row, so the retry is not blocked by dedupe."""
        stub_azure["error"] = "The photo was too blurry to read."
        post_photo(client)
        assert client.get(f"/api/invoices/{PARTITION}").status_code == 200

        stub_azure["error"] = None
        post_photo(client)
        assert int(stub_azure["runs"]) == 2
        body = client.get(f"/api/invoices/{PARTITION}").json()
        assert body["status"] == "complete"
        assert body["error"] is None, "the retry inherited the old failure"

    def test_a_successful_photo_is_not_reanalysed(
        self, client, stub_azure
    ) -> None:
        post_photo(client)
        post_photo(client)
        assert int(stub_azure["runs"]) == 1, "Document Intelligence bills per page"

    def test_a_running_job_is_not_started_twice(self, client, stub_azure) -> None:
        get_job_state().begin(PARTITION)
        post_photo(client)
        assert int(stub_azure["runs"]) == 0


def test_cors_advertises_every_method_the_api_accepts():
    """A browser blocks a cross-origin call whose method is not advertised.

    Hand-maintaining this list is how `DELETE` ended up missing from the
    preflight response, so it is checked against the routes themselves.
    """
    app = main_module.create_app()
    served = {
        method
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method not in {"HEAD", "OPTIONS"}
    }
    policy = next(
        args["allow_methods"]
        for args in (m.kwargs for m in app.user_middleware)
        if "allow_methods" in args
    )
    assert served <= set(policy), f"not advertised to browsers: {served - set(policy)}"


def test_two_submissions_of_one_photo_analyse_it_once(client, stub_azure, monkeypatch) -> None:
    """The dedupe check and the claim are not one atomic step.

    ``submit`` checks the store and the job state, then awaits the blob write
    before it calls ``begin``. Two requests for the same photo that overlap in
    that window both see "not started yet" and both queue an analysis, which
    bills Document Intelligence twice for one photo.
    """
    import threading

    both_inside = threading.Barrier(2, timeout=5)
    release = threading.Event()
    real_store = main_module.store_photo

    def slow_store(data, content_type, settings):
        # Hold the first request inside the window, and let the second one reach
        # the same point, so the two genuinely overlap.
        try:
            both_inside.wait()
        except threading.BrokenBarrierError:
            return real_store(data, content_type, settings)
        release.wait(timeout=5)
        return real_store(data, content_type, settings)

    monkeypatch.setattr(main_module, "store_photo", slow_store)

    codes: list[int] = []
    lock = threading.Lock()

    def submit() -> None:
        response = post_photo(client)
        with lock:
            codes.append(response.status_code)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert codes == [202, 202], f"unexpected responses: {codes}"
    assert int(stub_azure["runs"]) == 1, (
        f"the photo was analysed {stub_azure['runs']} times"
    )
