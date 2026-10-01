"""Guards against calling Azure SDK methods that do not exist.

Every bug this file refers to passed the full suite, because the fakes in
conftest defined methods the real SDK does not have. These tests compare the
fakes and the production call sites against the installed SDK instead of
trusting the fakes.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest
from azure.data.tables import TableClient, TableServiceClient

from app.config import Settings
from tests.conftest import FakeTableClient, FakeTableService

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _table_client_methods_used() -> set[str]:
    """Method names the production code calls on a table client.

    The client is always obtained as ``table = table_client(settings)`` and then
    used through that local name, so the calls are found by following the
    assignment rather than by looking for a chained call.
    """
    used: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            clients = {
                child.targets[0].id
                for child in ast.walk(node)
                if isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
                and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Name)
                and child.value.func.id == "table_client"
            }
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id in clients
                ):
                    used.add(child.func.attr)
    return used


def test_table_client_methods_used_by_the_app_exist_in_the_sdk() -> None:
    used = _table_client_methods_used()
    assert used, "expected to find methods called on a table_client(...) result"
    missing = {name for name in used if not hasattr(TableClient, name)}
    assert not missing, f"not methods on TableClient: {sorted(missing)}"


def test_table_service_methods_used_by_the_app_exist_in_the_sdk() -> None:
    used: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "table_service"
            ):
                used.add(node.attr)
    assert used, "expected to find table_service(...).<method> call sites"
    missing = {name for name in used if not hasattr(TableServiceClient, name)}
    assert not missing, f"not methods on TableServiceClient: {sorted(missing)}"


def test_fake_table_client_does_not_invent_methods() -> None:
    """The fake must not offer anything the SDK lacks.

    Inventing a method on the fake is exactly what let a broken production call
    site pass every test, so this is asserted explicitly rather than by review.
    """
    invented = {
        name
        for name in vars(FakeTableClient)
        if not name.startswith("_")
        and name
        not in {n for n in dir(TableClient) if not n.startswith("_")}
        and name not in {"entities"}
    }
    assert not invented, f"fake defines methods the SDK does not: {sorted(invented)}"


def test_fake_table_service_does_not_invent_methods() -> None:
    invented = {
        name
        for name in vars(FakeTableService)
        if not name.startswith("_")
        and name
        not in {n for n in dir(TableServiceClient) if not n.startswith("_")}
        and name not in {"_store"}
    }
    assert not invented, f"fake defines methods the SDK does not: {sorted(invented)}"


@pytest.mark.parametrize(
    "method",
    ["upsert_entity", "get_entity", "query_entities", "submit_transaction"],
)
def test_fake_covers_every_method_the_repository_needs(method: str) -> None:
    assert callable(getattr(FakeTableClient, method, None))


def test_the_fake_covers_every_table_method_the_app_calls() -> None:
    """The other direction: nothing the app calls may be missing from the fake.

    A method the app calls that the fake does not have would raise at runtime in
    the tests rather than fail them, so a missing fake is as dangerous as an
    invented one.
    """
    missing = {
        name
        for name in _table_client_methods_used()
        if not callable(getattr(FakeTableClient, name, None))
    }
    assert not missing, f"the fake does not cover: {sorted(missing)}"


def test_list_entities_is_never_called_with_a_partition_key() -> None:
    """Regression: the kwarg does not exist, so it reached the HTTP transport.

    ``list_entities(partition_key=...)`` is accepted by the old fake and
    rejected by the SDK, which forwards unknown kwargs to requests.
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "list_entities"
                and any(
                    kw.arg == "partition_key" for kw in node.keywords if kw.arg
                )
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"list_entities called with partition_key at {offenders}"


def test_repository_reads_whole_partitions_with_query_entities() -> None:
    from app import repository as repository_module

    assert callable(repository_module._partition_entities)


def test_download_blob_call_uses_only_declared_parameters() -> None:
    """BlobClient.download_blob ends in **kwargs, so a typo is not rejected.

    ``max_single_get_size`` looked plausible and was forwarded all the way to
    requests.Session.request, failing only at request time against real Azure.
    Comparing the call against the declared parameters catches that offline.
    """
    from azure.storage.blob import BlobClient

    declared = {
        name
        for name, param in inspect.signature(BlobClient.download_blob).parameters.items()
        if param.kind
        not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    }
    assert {"offset", "length"} <= declared

    from app.analyzer import MAX_BLOB_BYTES

    used = {"offset", "length"}
    assert used <= declared, f"not download_blob parameters: {sorted(used - declared)}"
    assert MAX_BLOB_BYTES > 0


def test_upload_blob_call_uses_only_real_parameters() -> None:
    """``upload_blob`` ends in ``**kwargs``, so a typo is not rejected.

    ``overwrite`` and ``content_settings`` are real options but are not declared
    parameters, so a signature check alone cannot tell them apart from an
    invented keyword that would be forwarded to the HTTP transport and only fail
    against Azure. The SDK's own option builder is therefore used as the source
    of truth for which keywords it consumes.
    """
    from azure.storage.blob import ContainerClient
    from azure.storage.blob._blob_client_helpers import _upload_blob_options

    declared = {
        name
        for name, param in inspect.signature(ContainerClient.upload_blob).parameters.items()
        if param.kind
        not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    }
    # Every keyword the production call site passes.
    used = {"name", "data", "overwrite", "content_settings"}
    # The option builder pops each of these, so the SDK handles them.
    consumed = set(
        re.findall(
            r"kwargs\.pop\('(\w+)'",
            inspect.getsource(_upload_blob_options),
        )
    )
    undocumented = {
        name
        for name in used
        if name not in declared and name not in consumed and name != "data"
    }
    assert not undocumented, (
        f"not real upload_blob parameters: {sorted(undocumented)}"
    )
