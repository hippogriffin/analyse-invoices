"""Shared fixtures.

The Azure clients are replaced with in-memory fakes so the suite never
contacts Azure and never needs credentials.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode
from fastapi.testclient import TestClient

from app import clients as clients_module
from app import config as config_module
from app import repository as repository_module
from app import storage as storage_module
from app.config import Settings, get_settings
from app.job_state import get_job_state

# Tests must never read a developer's real .env. Every setting is either passed
# explicitly or set through monkeypatch.setenv, so an actual backend/.env in the
# working tree cannot change the outcome of a test run.
Settings.model_config["env_file"] = None


class FakeTableClient:
    """Minimal stand-in for azure.data.tables.TableClient.

    Mirrors the behaviours the repository depends on: upsert replaces the row,
    merge updates only the given properties, list is partition-scoped and
    sorted by row key, and a missing row raises ResourceNotFoundError.

    Note the deliberate absence of ``create_table_if_not_exists``. The real
    TableClient has no such method; it belongs to TableServiceClient. Keeping
    the fake faithful to the real API surface is what stops a call to a
    non-existent SDK method from passing the suite.

    ``upsert_entity`` and ``delete_entity`` take a single entity, exactly as
    the SDK does. Batching only exists on ``submit_transaction``.
    """

    def __init__(self, store: dict[str, dict] | None = None) -> None:
        self.entities: dict[tuple[str, str], dict] = store if store is not None else {}
        self.submitted: list[list[tuple[str, dict]]] = []

    @staticmethod
    def _key(entity: dict) -> tuple[str, str]:
        return entity["PartitionKey"], entity["RowKey"]

    def upsert_entity(self, entity: dict) -> None:
        if not isinstance(entity, dict):
            raise TypeError(
                "upsert_entity takes a single entity; use submit_transaction to batch"
            )
        row = dict(self.entities.get(self._key(entity), {}))
        row.update(entity)
        self.entities[self._key(entity)] = row

    def submit_transaction(self, operations: list[tuple[str, dict]]) -> None:
        if len(operations) > 100:
            raise ValueError("a transaction holds at most 100 entities")
        self.submitted.append(list(operations))
        partitions = {self._key(entity)[0] for _, entity in operations}
        if len(partitions) > 1:
            raise ValueError("a transaction must stay inside one partition")
        # Staged first, so a failure leaves the store untouched like the service.
        staged: dict[tuple[str, str], dict | None] = {}
        for kind, entity in operations:
            if kind == "upsert":
                row = dict(self.entities.get(self._key(entity), {}))
                row.update(entity)
                staged[self._key(entity)] = row
            elif kind == "delete":
                staged[self._key(entity)] = None
            else:
                raise ValueError(f"unknown operation {kind!r}")
        for key, value in staged.items():
            if value is None:
                self.entities.pop(key, None)
            else:
                self.entities[key] = value

    def update_entity(
        self, entity: dict, mode: object = None, **kwargs: object
    ) -> None:
        """Real signature: update_entity(entity, mode=UpdateMode.MERGE).

        The mode is honoured rather than assumed. A fake that always merged
        would hide the failure mode that matters most here: a REPLACE drops
        every property the caller did not name, so a status-only update would
        quietly erase the row's identity fields and the test would still pass.
        """
        if mode is None:
            mode = UpdateMode.MERGE
        if mode not in (UpdateMode.REPLACE, UpdateMode.MERGE):
            raise ValueError(f"unknown update mode {mode!r}")
        key = self._key(entity)
        if key not in self.entities:
            raise ResourceNotFoundError("row not found")
        if mode is UpdateMode.MERGE:
            self.entities[key].update(entity)
        else:
            self.entities[key] = {**dict(zip(("PartitionKey", "RowKey"), key)), **entity}

    def get_entity(self, partition_key: str, row_key: str) -> dict:
        try:
            return dict(self.entities[(partition_key, row_key)])
        except KeyError:
            raise ResourceNotFoundError("row not found") from None

    def list_entities(self, *, results_per_page: int | None = None, select=None):
        # The real TableClient has no partition_key argument, so passing one
        # here must fail loudly rather than be silently accepted.
        for (partition, row_key), entity in sorted(self.entities.items()):
            yield dict(entity)

    def query_entities(
        self,
        query_filter: str,
        *,
        results_per_page: int | None = None,
        select=None,
        parameters: dict | None = None,
    ):
        """Supports only the parameterised PartitionKey equality used here."""
        match = re.fullmatch(r"\s*PartitionKey eq @(\w+)\s*", query_filter)
        if not match:
            raise NotImplementedError(
                f"fake query_entities only supports 'PartitionKey eq @param', "
                f"got {query_filter!r}"
            )
        if not parameters or match.group(1) not in parameters:
            raise ValueError("query parameter is missing")
        wanted = parameters[match.group(1)]
        for (partition, row_key), entity in sorted(self.entities.items()):
            if partition == wanted:
                yield dict(entity)

    def delete_entity(self, entity: dict) -> None:
        if not isinstance(entity, dict):
            raise TypeError(
                "delete_entity takes a single entity; use submit_transaction to batch"
            )
        self.entities.pop(self._key(entity), None)


class FakeTableService:
    """Stand-in for azure.data.tables.TableServiceClient.

    ``create_table_if_not_exists`` lives here, exactly as in the SDK, so the
    production ``ensure_table`` path is exercised by the tests.
    """

    def __init__(self, store: dict[str, dict]) -> None:
        self._store = store
        self.created_tables: list[str] = []

    def create_table_if_not_exists(self, table_name: str) -> None:
        self.created_tables.append(table_name)

    def get_table_client(self, table_name: str) -> FakeTableClient:
        return FakeTableClient(self._store)


class FakeBlobContainer:
    """Stand-in for azure.storage.blob.ContainerClient.

    Mirrors the single call the service makes: ``upload_blob`` with an explicit
    name, so a name the SDK would reject cannot pass the suite.
    """

    def __init__(self) -> None:
        self.url = "https://acct.blob.core.windows.net/pictures"
        self.blobs: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def upload_blob(
        self,
        *,
        name: str,
        data: bytes,
        overwrite: bool = False,
        content_settings=None,
    ) -> None:
        if not overwrite and name in self.blobs:
            raise ResourceExistsError("blob already exists")
        self.blobs[name] = bytes(data)
        if content_settings is not None:
            self.content_types[name] = content_settings.content_type


class FakeBlobClient:
    """Stand-in for azure.storage.blob.BlobClient.

    Only the operations the service performs on a stored photo are modelled.
    Deleting a blob that is not there raises, as the real client does, because
    "discard the photo" has to tell the difference between a delete that worked
    and one that silently did nothing.
    """

    def __init__(self, container: "FakeBlobContainer", name: str) -> None:
        self._container = container
        self._name = name

    def delete_blob(self, **kwargs: object) -> None:
        if kwargs:
            raise TypeError(f"unexpected delete_blob arguments: {sorted(kwargs)}")
        if self._name not in self._container.blobs:
            raise ResourceNotFoundError("blob not found")
        del self._container.blobs[self._name]
        self._container.content_types.pop(self._name, None)


class FakeBlobService:
    """Stand-in for BlobServiceClient."""

    def __init__(self, container: FakeBlobContainer) -> None:
        self._container = container

    def get_container_client(self, container: str) -> FakeBlobContainer:
        return self._container

    def get_blob_client(self, container: str, blob: str, **_: object) -> FakeBlobClient:
        return FakeBlobClient(self._container, blob)


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    get_settings.cache_clear()
    # Confidence lives in the process, so it has to be cleared between tests or a
    # result from one test would satisfy another's assertion.
    get_job_state().clear()
    yield
    get_settings.cache_clear()
    get_job_state().clear()


@pytest.fixture
def blob_container() -> FakeBlobContainer:
    return FakeBlobContainer()


@pytest.fixture
def table_store() -> dict[str, dict]:
    return {}


@pytest.fixture
def table(table_store: dict[str, dict]) -> FakeTableClient:
    return FakeTableClient(table_store)


@pytest.fixture
def table_service(table_store: dict[str, dict]) -> FakeTableService:
    return FakeTableService(table_store)


@pytest.fixture
def settings(
    monkeypatch: pytest.MonkeyPatch,
    table: FakeTableClient,
    table_service: FakeTableService,
    blob_container: FakeBlobContainer,
) -> object:
    monkeypatch.setenv("STORAGE_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    monkeypatch.setenv("SOURCE_CONTAINER", "pictures")
    monkeypatch.setenv("SOURCE_PREFIX", "2026")
    monkeypatch.setenv("RESULT_TABLE_NAME", "invoices")
    monkeypatch.setenv(
        "DOCUMENT_INTELLIGENCE_ENDPOINT", "https://di.cognitiveservices.azure.com"
    )
    monkeypatch.delenv("BLOB_AUTH_METHOD", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("DOCUMENT_INTELLIGENCE_KEY", raising=False)
    monkeypatch.setattr(repository_module, "table_client", lambda *_a, **_k: table)
    monkeypatch.setattr(
        clients_module, "table_service", lambda *_a, **_k: table_service
    )
    monkeypatch.setattr(
        storage_module,
        "blob_service",
        lambda *_a, **_k: FakeBlobService(blob_container),
    )
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def client(settings: object) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
