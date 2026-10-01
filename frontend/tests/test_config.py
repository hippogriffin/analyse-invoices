from __future__ import annotations

from app.config import Settings


def test_settings_are_hashable() -> None:
    assert isinstance(hash(Settings()), int)


def test_analysis_api_base_url_defaults_to_disabled() -> None:
    assert Settings().analysis_api_base_url == ""
    assert Settings().is_analysis_configured is False


def test_analysis_api_base_url_is_configurable() -> None:
    settings = Settings(analysis_api_base_url="https://analysis.example")

    assert settings.analysis_api_base_url == "https://analysis.example"
    assert settings.is_analysis_configured is True


def test_trailing_slashes_are_stripped_from_the_base_url() -> None:
    """The UI appends "/api/invoices", so a trailing slash would double up."""
    settings = Settings(analysis_api_base_url="https://analysis.example///")

    assert settings.analysis_api_base_url == "https://analysis.example"


def test_whitespace_around_the_base_url_is_stripped() -> None:
    settings = Settings(analysis_api_base_url="  https://analysis.example  ")

    assert settings.analysis_api_base_url == "https://analysis.example"


def test_no_azure_settings_remain() -> None:
    """This service must not be able to hold storage or DI credentials.

    The backend owns every Azure call, so a credential field here would be
    dead configuration that looks like it does something.
    """
    forbidden = {
        "storage_account_url",
        "storage_account_name",
        "blob_container",
        "blob_prefix",
        "blob_auth_method",
        "azure_client_id",
        "azure_storage_account_key",
        "document_intelligence_endpoint",
        "document_intelligence_key",
    }
    assert not forbidden & set(Settings.model_fields), (
        f"the UI service still defines Azure settings: "
        f"{sorted(forbidden & set(Settings.model_fields))}"
    )


def test_size_limit_defaults_and_is_configurable() -> None:
    assert Settings().max_upload_bytes == 10 * 1024 * 1024
    assert Settings(max_upload_bytes=2048).max_upload_bytes == 2048


def test_content_types_default_to_the_supported_images() -> None:
    assert Settings().allowed_content_types == (
        "image/jpeg",
        "image/png",
        "image/webp",
    )


def test_content_types_parsed_from_comma_separated_string() -> None:
    assert Settings(allowed_content_types="image/jpeg, image/png").allowed_content_types == (
        "image/jpeg",
        "image/png",
    )


def test_content_types_parsed_from_environment(monkeypatch) -> None:
    """Regression: a tuple field is JSON-decoded unless NoDecode is set."""
    monkeypatch.setenv("ALLOWED_CONTENT_TYPES", "image/jpeg,image/png")

    assert Settings().allowed_content_types == ("image/jpeg", "image/png")


def test_content_types_are_lowercased() -> None:
    """A file reported as IMAGE/JPEG must still be accepted."""
    assert Settings(allowed_content_types="IMAGE/JPEG, Image/PNG").allowed_content_types == (
        "image/jpeg",
        "image/png",
    )


def test_a_missing_analysis_service_warns(monkeypatch, caplog) -> None:
    """With no backend there is nowhere to send a photo, so say so loudly."""
    monkeypatch.delenv("ANALYSIS_API_BASE_URL", raising=False)
    with caplog.at_level("WARNING"):
        Settings()
    assert "ANALYSIS_API_BASE_URL" in caplog.text
