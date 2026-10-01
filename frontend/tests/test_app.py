"""The UI service serves pages and configuration, and nothing else.

There is no upload endpoint here any more: the browser posts the photo straight
to the analysis backend, which owns storage and Document Intelligence.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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

    assert body["analysisApiBaseUrl"] == ""
    assert body["analysisConfigured"] is False


def test_config_passes_the_analysis_base_url(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("ANALYSIS_API_BASE_URL", "https://analysis.example")
    get_settings.cache_clear()

    body = client.get("/api/config").json()

    assert body["analysisApiBaseUrl"] == "https://analysis.example"
    assert body["analysisConfigured"] is True


def test_there_is_no_upload_endpoint(client: TestClient):
    """Regression risk: a stub endpoint would invite the old two-step flow.

    The UI has no storage, so accepting a file here would mean storing it
    somewhere it cannot reach.
    """
    assert client.post("/api/upload", files={"photo": ("a.jpg", b"x", "image/jpeg")}).status_code == 404


def test_the_service_exposes_no_azure_routes(client: TestClient):
    """This process must never be the thing that talks to Azure."""
    paths = {route.path for route in app.routes}
    assert paths == {"/", "/healthz", "/api/config", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/static"}


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
