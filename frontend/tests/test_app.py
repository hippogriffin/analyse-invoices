"""The UI service serves pages and configuration, and proxies the API calls.

There is no storage here: the browser posts the photo to this service, which
forwards it to the analysis backend. The backend owns storage and Document
Intelligence, and its address never reaches the browser.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import proxy
from app.config import get_settings
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_index_is_served(client: TestClient):
    response = client.get("/")

    assert response.status_code == 200
    assert "Invoice Upload" in response.text


def test_static_assets_are_served(client: TestClient):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_healthz(client: TestClient):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_reports_the_upload_limits(client: TestClient):
    """The UI needs these to filter the file chooser and reject a bad file."""
    body = client.get("/api/config").json()

    assert body["maxUploadBytes"] == 10 * 1024 * 1024
    assert "image/jpeg" in body["allowedContentTypes"]


def test_config_reports_no_analysis_service_by_default(client: TestClient):
    body = client.get("/api/config").json()

    assert body["analysisConfigured"] is False


def test_config_reports_a_configured_backend_without_leaking_its_address(
    client: TestClient, monkeypatch
):
    """The browser must not be handed the backend address: it posts here instead.

    A cluster-internal name would leak the deployment's topology, and the browser
    could not resolve it anyway, which is exactly what the proxy is for.
    """
    monkeypatch.setenv(
        "ANALYSIS_API_BASE_URL", "http://invoice-analysis.default.svc.cluster.local:3333"
    )
    get_settings.cache_clear()

    body = client.get("/api/config").json()

    assert body["analysisConfigured"] is True
    assert "analysisApiBaseUrl" not in body
    assert "svc.cluster.local" not in client.get("/api/config").text


def test_there_is_no_upload_endpoint(client: TestClient):
    """Regression risk: a stub endpoint would invite the old two-step flow.

    The UI has no storage, so accepting a file here would mean storing it
    somewhere it cannot reach.
    """
    assert client.post("/api/upload", files={"photo": ("a.jpg", b"x", "image/jpeg")}).status_code == 404


def _all_route_paths(routes) -> set[str]:
    """Flatten every registered path, including mounted and included routers."""
    found: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            found.add(path)
        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None)
        if nested:
            found |= _all_route_paths(nested)
    return found


def test_the_service_exposes_no_azure_routes(client: TestClient):
    """This process must never be the thing that talks to Azure."""
    paths = _all_route_paths(app.routes)
    assert paths == {
        "/",
        "/healthz",
        "/api/config",
        "/api/invoices",
        "/api/invoices/{invoice_id}",
        "/api/invoices/{invoice_id}/photo",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/static",
    }


def test_serving_a_page_imports_no_azure_sdk():
    """Regression guard for the service split.

    The UI used to upload to Blob Storage itself, which is why a storage key
    sat in this service. Importing the SDK here is the first step back, so the
    boundary is asserted rather than left to review.
    """
    code = (
        "import app.main, sys;"
        "assert 'azure' not in sys.modules, sorted(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, f"the UI service pulled in an Azure SDK: {result.stderr}"


# ------------------------------------------------------------------- proxy


BACKEND = "http://invoice-analysis.default.svc.cluster.local:3333"


class _RecordingClient:
    """Stands in for ``httpx.AsyncClient``, recording the forwarded request.

    The proxy is the seam between the browser and the backend, so what matters
    is that the method, target URL and photo body arrive at the backend intact.
    """

    calls: list[dict] = []
    status_code = 200
    body = {"status": "complete"}

    def __init__(self, *args, **kwargs) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def request(self, method, url, files=None):
        type(self).calls.append({"method": method, "url": url, "files": files})
        return httpx.Response(
            type(self).status_code,
            json=type(self).body,
            headers={"content-type": "application/json"},
        )


@pytest.fixture
def recorded_backend(monkeypatch):
    _RecordingClient.calls = []
    _RecordingClient.status_code = 200
    _RecordingClient.body = {"status": "complete"}
    monkeypatch.setattr(proxy.httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setenv("ANALYSIS_API_BASE_URL", BACKEND)
    get_settings.cache_clear()
    yield _RecordingClient
    get_settings.cache_clear()


def test_submit_is_forwarded_to_the_backend(client: TestClient, recorded_backend):
    response = client.post(
        "/api/invoices", files={"photo": ("march.jpg", b"JPEGDATA", "image/jpeg")}
    )

    assert response.status_code == 200
    call = recorded_backend.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == f"{BACKEND}/api/invoices"
    filename, content, content_type = call["files"]["photo"]
    assert filename == "march.jpg"
    assert content == b"JPEGDATA"
    assert content_type == "image/jpeg"


def test_status_and_verdict_calls_are_forwarded(client: TestClient, recorded_backend):
    client.get("/api/invoices/inv-1")
    client.post("/api/invoices/inv-1/photo")
    client.delete("/api/invoices/inv-1/photo")

    assert [(call["method"], call["url"]) for call in recorded_backend.calls] == [
        ("GET", f"{BACKEND}/api/invoices/inv-1"),
        ("POST", f"{BACKEND}/api/invoices/inv-1/photo"),
        ("DELETE", f"{BACKEND}/api/invoices/inv-1/photo"),
    ]


def test_the_backends_status_and_body_are_passed_back(
    client: TestClient, recorded_backend
):
    """A rejected photo must still read as a JSON error for the UI."""
    recorded_backend.status_code = 502
    recorded_backend.body = {"detail": "Blob storage rejected the write."}

    response = client.get("/api/invoices/inv-1")

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "Blob storage rejected the write."


def test_a_missing_backend_is_reported_as_unavailable(client: TestClient):
    """With no backend there is nowhere to forward, so refuse before buffering."""
    response = client.post(
        "/api/invoices", files={"photo": ("a.jpg", b"JPEGDATA", "image/jpeg")}
    )

    assert response.status_code == 503


def test_an_unreachable_backend_is_a_gateway_error(client: TestClient, monkeypatch):
    class _Unreachable:
        def __init__(self, *args, **kwargs) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Unreachable)
    monkeypatch.setenv("ANALYSIS_API_BASE_URL", "http://backend:3333")
    get_settings.cache_clear()

    response = client.get("/api/invoices/inv-1")

    assert response.status_code == 502
    assert "Could not reach" in response.json()["detail"]
    get_settings.cache_clear()


def test_an_oversized_upload_is_refused_before_it_reaches_the_backend(
    client: TestClient, recorded_backend, monkeypatch
):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4")
    get_settings.cache_clear()

    response = client.post(
        "/api/invoices", files={"photo": ("a.jpg", b"far too big", "image/jpeg")}
    )

    assert response.status_code == 413
    assert recorded_backend.calls == []
