"""Field extraction from the Document Intelligence response."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.analyzer import AnalysisError, NotConfiguredError, _read_field, _to_documents
from app.config import Settings


@dataclass
class Field:
    content: str = ""
    confidence: float = 0.9
    value_string: str | None = None
    value_date: str | None = None
    value_currency: object | None = None
    value_number: float | None = None
    value_integer: int | None = None
    value_boolean: bool | None = None
    value_selection_group: str | None = None
    value_address: object | None = None
    value_phone_number: str | None = None
    value_country_region: str | None = None
    value_time: str | None = None
    value_object: object | None = None
    value_array: object | None = None
    values: list = field(default_factory=list)


@dataclass
class Document:
    doc_type: str = "invoice"
    confidence: float = 0.95
    fields: dict = field(default_factory=dict)


@dataclass
class Result:
    documents: list


class TestDownloadPhoto:
    """download_photo used an option BlobClient.download_blob does not accept.

    The invalid keyword was forwarded to the HTTP transport, so the read failed
    at request time rather than at the call site. The fake blob client accepted
    **kwargs, so the suite did not catch it.
    """

    @staticmethod
    def _blob(payload: bytes, recorded: list[dict]) -> object:
        class Downloader:
            def readall(self) -> bytes:
                return payload

        class Blob:
            def download_blob(self, **kwargs: object):
                recorded.append(dict(kwargs))
                return Downloader()

        class Service:
            def get_blob_client(self, container: str, blob: str, **_: object) -> Blob:
                return Blob()

        return Service()

    def test_reads_the_photo(self, settings, monkeypatch: pytest.MonkeyPatch) -> None:
        from app import analyzer as analyzer_module

        recorded: list[dict] = []
        monkeypatch.setattr(
            analyzer_module,
            "blob_service",
            lambda *_a, **_k: self._blob(b"jpeg-bytes", recorded),
        )
        assert analyzer_module.download_photo(settings, "2026/abc.jpg") == b"jpeg-bytes"

    def test_requests_only_a_bounded_range(self, settings, monkeypatch: pytest.MonkeyPatch) -> None:
        """The length is the SDK's own range parameter, not an invented option.

        Pinned to the exact call, because download_blob forwards unknown
        keywords to the HTTP transport instead of rejecting them.
        """
        from app import analyzer as analyzer_module

        recorded: list[dict] = []
        monkeypatch.setattr(
            analyzer_module,
            "blob_service",
            lambda *_a, **_k: self._blob(b"x", recorded),
        )
        analyzer_module.download_photo(settings, "2026/abc.jpg")
        assert recorded == [
            {"offset": 0, "length": analyzer_module.MAX_BLOB_BYTES + 1}
        ], "must use only real download_blob parameters"
        assert "max_single_get_size" not in recorded[0]

    def test_rejects_an_oversized_photo(self, settings, monkeypatch: pytest.MonkeyPatch) -> None:
        from app import analyzer as analyzer_module

        oversized = b"x" * (analyzer_module.MAX_BLOB_BYTES + 1)
        monkeypatch.setattr(
            analyzer_module,
            "blob_service",
            lambda *_a, **_k: self._blob(oversized, []),
        )
        with pytest.raises(AnalysisError, match="larger than"):
            analyzer_module.download_photo(settings, "2026/abc.jpg")

    def test_refuses_a_blob_outside_the_prefix(
        self, settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The name comes from a table row, so a foreign one must not be read."""
        from app import analyzer as analyzer_module

        def must_not_be_called(*_a, **_k) -> object:
            raise AssertionError("the blob service must not be reached")

        monkeypatch.setattr(analyzer_module, "blob_service", must_not_be_called)
        with pytest.raises(AnalysisError, match="Refusing to read"):
            analyzer_module.download_photo(settings, "../elsewhere/secret.jpg")


class TestReadField:
    def test_reads_string(self) -> None:
        assert _read_field(Field(value_string="Acme")) == ("Acme", 0.9)

    def test_reads_date(self) -> None:
        assert _read_field(Field(value_date="2026-03-01"))[0] == "2026-03-01"

    def test_currency_wins_over_number(self) -> None:
        # InvoiceTotal carries both; picking the number would lose the currency.
        class Currency:
            amount = 10.0
            code = "GBP"

        value, _ = _read_field(
            Field(value_currency=Currency(), value_number=10.0, value_string="x")
        )
        assert isinstance(value, Currency)

    def test_selection_group_wins_over_raw_content(self) -> None:
        value, _ = _read_field(Field(content="raw", value_selection_group="NET30"))
        assert value == "NET30"

    def test_falls_back_to_content(self) -> None:
        value, _ = _read_field(Field(content="some text"))
        assert value == "some text"

    def test_missing_everything_is_empty_not_none(self) -> None:
        value, _ = _read_field(Field(content=""))
        assert value == ""

    def test_confidence_is_returned(self) -> None:
        _, confidence = _read_field(Field(value_string="x", confidence=0.42))
        assert confidence == 0.42


class TestToDocuments:
    def test_no_documents_is_empty(self) -> None:
        assert _to_documents(Result(documents=[])) == []

    def test_missing_documents_attribute_is_tolerated(self) -> None:
        assert _to_documents(object()) == []

    def test_fields_are_captured_in_name_order(self) -> None:
        result = Result(
            documents=[
                Document(
                    fields={
                        "VendorName": Field(value_string="Acme", confidence=0.99),
                        "InvoiceTotal": Field(value_string="99.00"),
                        "InvoiceId": Field(value_string="INV-1"),
                    }
                )
            ]
        )
        documents = _to_documents(result)
        assert len(documents) == 1
        assert [item.name for item in documents[0].fields] == [
            "InvoiceId",
            "InvoiceTotal",
            "VendorName",
        ]
        assert documents[0].index == 0

    def test_indices_are_sequential(self) -> None:
        result = Result(documents=[Document(fields={}), Document(fields={})])
        assert [doc.index for doc in _to_documents(result)] == [0, 1]

    def test_document_with_no_fields_is_kept(self) -> None:
        result = Result(documents=[Document(fields={})])
        assert _to_documents(result)[0].fields == []


class TestAnalyzeGuards:
    def test_missing_endpoint_is_a_configuration_error(self) -> None:
        with pytest.raises(NotConfiguredError):
            from app.analyzer import analyze

            analyze(Settings(), b"bytes")

    def test_blank_endpoint_is_a_configuration_error(self) -> None:
        with pytest.raises(NotConfiguredError):
            from app.analyzer import analyze

            analyze(
                Settings(
                    document_intelligence_endpoint="",
                    storage_account_url="https://a.blob.core.windows.net",
                ),
                b"bytes",
            )


def test_analysis_error_is_raisable() -> None:
    assert str(AnalysisError("boom")) == "boom"
