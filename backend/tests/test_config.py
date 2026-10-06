"""Configuration, authentication and blob-scope checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AuthMethod, Settings, get_settings


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "STORAGE_ACCOUNT_URL": "https://acct.blob.core.windows.net",
        "DOCUMENT_INTELLIGENCE_ENDPOINT": "https://di.cognitiveservices.azure.com/",
    }
    return {**base, **overrides}


def test_trailing_slashes_are_stripped() -> None:
    settings = Settings(**_env())
    assert settings.document_intelligence_endpoint == "https://di.cognitiveservices.azure.com"


def test_account_name_derived_from_url() -> None:
    assert Settings(**_env()).resolved_account_name == "acct"


def test_account_name_from_privatelink_url() -> None:
    settings = Settings(
        **{
            **_env(),
            "STORAGE_ACCOUNT_URL": "https://acct.privatelink.blob.core.windows.net",
        }
    )
    assert settings.resolved_account_name == "acct"


def test_account_name_overrides_url() -> None:
    settings = Settings(**_env(STORAGE_ACCOUNT_NAME="explicit"))
    assert settings.resolved_account_name == "explicit"


def test_managed_identity_is_the_default() -> None:
    settings = Settings(**_env())
    assert settings.blob_auth_method is AuthMethod.MANAGED_IDENTITY
    assert not settings.uses_account_key


def test_is_configured_requires_both_services() -> None:
    assert Settings(**_env()).is_configured
    assert not Settings(STORAGE_ACCOUNT_URL="https://acct.blob.core.windows.net").is_configured


def test_account_key_requires_a_key() -> None:
    with pytest.raises(ValidationError, match="AZURE_STORAGE_ACCOUNT_KEY"):
        Settings(**_env(BLOB_AUTH_METHOD="account_key"))


def test_account_key_requires_document_intelligence_key() -> None:
    with pytest.raises(ValidationError, match="DOCUMENT_INTELLIGENCE_KEY"):
        Settings(
            **_env(
                BLOB_AUTH_METHOD="account_key",
                AZURE_STORAGE_ACCOUNT_KEY="storage-key",
            )
        )


def test_account_key_with_both_keys_is_accepted() -> None:
    settings = Settings(
        **_env(
            BLOB_AUTH_METHOD="account_key",
            AZURE_STORAGE_ACCOUNT_KEY="storage-key",
            DOCUMENT_INTELLIGENCE_KEY="di-key",
        )
    )
    assert settings.uses_account_key
    assert settings.document_intelligence_key.get_secret_value() == "di-key"


def test_cors_origins_split_from_csv() -> None:
    settings = Settings(**_env(CORS_ALLOW_ORIGINS="https://a.example, https://b.example"))
    assert settings.cors_allow_origins == ("https://a.example", "https://b.example")


def test_credentials_rejected_with_wildcard_origin() -> None:
    with pytest.raises(ValidationError, match="CORS_ALLOW_CREDENTIALS"):
        Settings(**_env(CORS_ALLOW_ORIGINS="*", CORS_ALLOW_CREDENTIALS="true"))


def test_settings_are_hashable_for_credential_caching() -> None:
    assert hash(Settings(**_env())) == hash(Settings(**_env()))


class TestBlobScope:
    """The API is unauthenticated, so blob access must stay inside its lane."""

    def test_accepts_named_blob_under_prefix(self, settings: object) -> None:
        assert get_settings().accepts_blob("2026/abc123.jpg")

    def test_rejects_blob_outside_prefix(self, settings: object) -> None:
        assert not get_settings().accepts_blob("2025/abc123.jpg")

    def test_rejects_traversal(self, settings: object) -> None:
        assert not get_settings().accepts_blob("2026/../../secret.jpg")

    def test_rejects_empty(self, settings: object) -> None:
        assert not get_settings().accepts_blob("   ")

    def test_leading_slash_is_tolerated(self, settings: object) -> None:
        assert get_settings().accepts_blob("/2026/abc123.jpg")

    def test_without_prefix_any_name_in_container_is_allowed(self, monkeypatch) -> None:
        monkeypatch.setenv("STORAGE_ACCOUNT_URL", "https://acct.blob.core.windows.net")
        monkeypatch.setenv("SOURCE_CONTAINER", "pictures")
        monkeypatch.delenv("SOURCE_PREFIX", raising=False)
        get_settings.cache_clear()
        assert get_settings().accepts_blob("anything.jpg")
        assert not get_settings().accepts_blob("../elsewhere.jpg")


class TestConfigurationWarnings:
    """A wrong-service endpoint produces an Azure error naming no setting.

    Azure answers "The resource doesn't support specified Http Verb", which does
    not say which setting is wrong, so the endpoint is checked up front.
    """

    def test_missing_settings_are_named(self) -> None:
        warnings = Settings().config_warnings()
        assert len(warnings) == 1
        assert "DOCUMENT_INTELLIGENCE_ENDPOINT" in warnings[0]
        assert "503" in warnings[0]

    def test_valid_endpoint_produces_no_warning(self) -> None:
        settings = Settings(**_env())
        assert settings.config_warnings() == []

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://acct.blob.core.windows.net",
            "https://acct.table.core.windows.net",
            "https://acct.dfs.core.windows.net",
            "https://acct.search.windows.net",
        ],
    )
    def test_wrong_service_endpoint_is_flagged(self, endpoint: str) -> None:
        settings = Settings(**_env(DOCUMENT_INTELLIGENCE_ENDPOINT=endpoint))
        assert not settings.document_intelligence_endpoint_looks_valid
        assert any("Http Verb" in w for w in settings.config_warnings())

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://di.cognitiveservices.azure.com",
            "https://di.services.ai.azure.com",
        ],
    )
    def test_known_document_intelligence_hosts_are_accepted(
        self, endpoint: str
    ) -> None:
        settings = Settings(**_env(DOCUMENT_INTELLIGENCE_ENDPOINT=endpoint))
        assert settings.document_intelligence_endpoint_looks_valid

    def test_legacy_regional_host_is_rejected_with_an_explanation(self) -> None:
        """The Form Recognizer host answers the GA path with 404.

        Accepting it produced a bare "unsupported Http Verb" at analysis time
        with nothing pointing at the setting at fault.
        """
        endpoint = "https://northeurope.api.cognitive.microsoft.com"
        settings = Settings(**_env(DOCUMENT_INTELLIGENCE_ENDPOINT=endpoint))

        assert not settings.document_intelligence_endpoint_looks_valid
        assert settings.uses_legacy_di_endpoint
        warning = settings.config_warnings()[0]
        assert "legacy multi-service" in warning
        assert "https://<resource-name>.cognitiveservices.azure.com" in warning

    def test_non_https_storage_url_is_flagged(self) -> None:
        settings = Settings(
            **{**_env(), "STORAGE_ACCOUNT_URL": "http://acct.blob.core.windows.net"}
        )
        assert any("not https" in w for w in settings.config_warnings())


class TestTableEndpoint:
    """Regression: table operations sent to the blob host return 405.

    Blob and Table are separate services on separate hosts. The blob service
    rejects a table entity write with ``UnsupportedHttpVerb``, which gives no
    hint that the endpoint is wrong, so the derivation is asserted here.
    """

    def test_derived_from_the_account_name(self) -> None:
        settings = Settings(**_env())
        assert settings.table_storage_url == "https://acct.table.core.windows.net"

    def test_differs_from_the_blob_endpoint(self) -> None:
        settings = Settings(**_env())
        assert settings.table_storage_url != settings.storage_account_url

    def test_privatelink_blob_url_keeps_the_privatelink_table_host(self) -> None:
        settings = Settings(
            **{
                **_env(),
                "STORAGE_ACCOUNT_URL": "https://acct.privatelink.blob.core.windows.net",
            }
        )
        assert settings.table_storage_url == "https://acct.privatelink.table.core.windows.net"

    def test_explicit_account_name_is_used(self) -> None:
        settings = Settings(**_env(STORAGE_ACCOUNT_NAME="other-account"))
        assert settings.table_storage_url == "https://other-account.table.core.windows.net"

    def test_trailing_slash_does_not_leak(self) -> None:
        settings = Settings(**_env(STORAGE_ACCOUNT_URL="https://acct.blob.core.windows.net/"))
        assert settings.table_storage_url == "https://acct.table.core.windows.net"

    def test_emulator_endpoint_passes_through(self) -> None:
        settings = Settings(
            **_env(STORAGE_ACCOUNT_URL="http://127.0.0.1:10002/devstoreaccount1")
        )
        assert settings.table_storage_url == "http://127.0.0.1:10002/devstoreaccount1"

    def test_empty_when_storage_url_is_missing(self) -> None:
        assert Settings().table_storage_url == ""


def test_table_client_uses_the_table_endpoint() -> None:
    """The SDK must never be handed the blob host for a table operation."""
    from app.clients import table_client

    client = table_client(Settings(**_env()))
    assert client.url.startswith("https://acct.table.core.windows.net")
    assert "blob.core" not in client.url


def test_table_client_without_storage_url_is_a_configuration_error() -> None:
    from app.clients import NotConfiguredError, table_client

    with pytest.raises(NotConfiguredError, match="Table Storage endpoint"):
        table_client(Settings())


def test_real_dotenv_never_leaks_into_settings() -> None:
    """Guards the conftest isolation.

    backend/.env holds real Azure values. A Settings() built inside a test must
    not pick them up, or the suite's result would depend on local configuration.
    """
    assert Settings().storage_account_url == ""
    assert Settings().document_intelligence_endpoint == ""


class TestUploadLimits:
    """The service owns the limits now, so it must enforce and publish them."""

    def test_default_size_limit(self) -> None:
        assert Settings().max_upload_bytes == 10 * 1024 * 1024

    def test_size_limit_is_configurable(self) -> None:
        assert Settings(max_upload_bytes=2048).max_upload_bytes == 2048

    def test_content_types_default_to_the_supported_images(self) -> None:
        assert Settings().allowed_content_types == (
            "image/jpeg",
            "image/png",
            "image/webp",
        )

    def test_content_types_parse_from_a_comma_separated_string(self) -> None:
        """Regression: a tuple field is JSON-decoded unless NoDecode is set."""
        assert Settings(
            allowed_content_types="image/jpeg, image/png"
        ).allowed_content_types == ("image/jpeg", "image/png")

    def test_content_types_are_lowercased(self) -> None:
        """An upload sent as Content-Type: IMAGE/JPEG must still be accepted."""
        assert Settings(
            allowed_content_types="IMAGE/JPEG, Image/PNG"
        ).allowed_content_types == ("image/jpeg", "image/png")

    def test_origins_keep_their_case(self) -> None:
        """Unlike content types, origins are case sensitive."""
        assert Settings(cors_allow_origins="HTTPS://App.Example.com").cors_allow_origins == (
            "HTTPS://App.Example.com",
        )
