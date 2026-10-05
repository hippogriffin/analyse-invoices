"""Application configuration, read from environment variables."""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")

BLOB_HOST_PATTERN = re.compile(r"^(?P<account>[^.]+)\.(?:privatelink\.)?blob\.core\.")

# The GA Document Intelligence SDK talks to a resource endpoint. The legacy
# multi-service regional host (*.api.cognitive.microsoft.com) serves Form
# Recognizer v2.1 and answers the GA /documentintelligence path with 404, so it
# is rejected up front rather than failing as "unsupported Http Verb" mid-request.
DI_ENDPOINT_SUFFIXES = (
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
)
LEGACY_DI_ENDPOINT_SUFFIXES = (".api.cognitive.microsoft.com",)

BLOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,1000}$")


class AuthMethod(StrEnum):
    MANAGED_IDENTITY = "managed_identity"
    ACCOUNT_KEY = "account_key"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


# Row key of the first document, which is also the entity that says the invoice
# finished. Later pages get doc-001, doc-002 and so on. See app.repository.
FIRST_DOC_ROW_KEY = "doc-000"


class Settings(BaseSettings):
    # Frozen so instances are hashable: app.clients caches credentials on them.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Source photo -------------------------------------------------------
    storage_account_url: str = Field(
        default="",
        description="Blob endpoint holding the captured photos.",
    )
    storage_account_name: str = Field(default="")
    source_container: str = Field(
        default="pictures",
        description="Container the submitted photos are written to.",
    )
    source_prefix: str = Field(
        default="",
        description=(
            "Optional prefix every generated blob name sits under, e.g. 2026. "
            "Also constrains which blobs this service will analyse."
        ),
    )
    max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        description="Largest accepted photo, enforced while streaming the upload.",
    )
    # NoDecode: the value arrives as a raw comma-separated string from env/.env
    # and is split by the validator below, instead of being JSON-decoded.
    allowed_content_types: Annotated[
        tuple[str, ...], NoDecode, Field(default=DEFAULT_ALLOWED_CONTENT_TYPES)
    ]

    # Results ------------------------------------------------------------
    result_table_name: str = Field(
        default="invoices",
        description="Table Storage table that holds job state and extracted fields.",
    )

    # Document Intelligence ----------------------------------------------
    document_intelligence_endpoint: str = Field(
        default="",
        description="e.g. https://<resource>.cognitiveservices.azure.com/",
    )
    document_intelligence_model: str = Field(default="prebuilt-invoice")
    document_intelligence_key: SecretStr = Field(
        default=SecretStr(""),
        description="Document Intelligence API key. Only used with account_key auth.",
    )
    analysis_timeout_seconds: int = Field(
        default=120,
        description="How long to wait for the analyze operation to finish.",
    )

    # Behaviour ----------------------------------------------------------
    cors_allow_origins: Annotated[tuple[str, ...], NoDecode, Field(default=("*",))]
    cors_allow_credentials: bool = Field(default=False)

    # Authentication -----------------------------------------------------
    blob_auth_method: AuthMethod = Field(default=AuthMethod.MANAGED_IDENTITY)
    azure_client_id: str = Field(default="")
    azure_storage_account_key: SecretStr = Field(default=SecretStr(""))

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip() for item in value)
        return value

    @field_validator("allowed_content_types", mode="before")
    @classmethod
    def _split_types(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip() for item in value)
        return value

    @field_validator("allowed_content_types", mode="after")
    @classmethod
    def _lowercase_content_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Compared against the request header, so case is normalised.

        Origins are deliberately left alone: they are case sensitive.
        """
        return tuple(item.lower() for item in value)

    @field_validator("storage_account_url", "document_intelligence_endpoint")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _check_configuration(self) -> Settings:
        if self.cors_allow_credentials and "*" in self.cors_allow_origins:
            raise ValueError(
                "CORS_ALLOW_CREDENTIALS cannot be used with a '*' origin. "
                "List explicit origins in CORS_ALLOW_ORIGINS instead."
            )
        if self.blob_auth_method is AuthMethod.ACCOUNT_KEY:
            if not self.azure_storage_account_key.get_secret_value():
                raise ValueError(
                    "BLOB_AUTH_METHOD=account_key requires AZURE_STORAGE_ACCOUNT_KEY."
                )
            if not self.document_intelligence_key.get_secret_value():
                raise ValueError(
                    "BLOB_AUTH_METHOD=account_key requires DOCUMENT_INTELLIGENCE_KEY."
                )
            logger.warning(
                "Using API keys for Azure services. This is for local testing only."
            )
        return self

    @property
    def is_configured(self) -> bool:
        return bool(
            self.storage_account_url
            and self.document_intelligence_endpoint
            and self.source_container
            and self.result_table_name
        )

    @property
    def uses_account_key(self) -> bool:
        return self.blob_auth_method is AuthMethod.ACCOUNT_KEY

    @property
    def resolved_account_name(self) -> str:
        if self.storage_account_name:
            return self.storage_account_name.strip()
        if not self.storage_account_url:
            return ""
        parsed = urlparse(self.storage_account_url)
        segments = [part for part in parsed.path.split("/") if part]
        if segments:
            return segments[0]
        match = BLOB_HOST_PATTERN.match(parsed.hostname or "")
        return match.group("account") if match else ""

    @property
    def table_storage_url(self) -> str:
        """Table Storage endpoint, which is not the blob endpoint.

        Blob and Table are separate services on separate hosts. Sending a table
        operation to ``*.blob.core.windows.net`` reaches the blob service, which
        rejects it with ``UnsupportedHttpVerb`` (405), because table entities
        need a verb the blob service does not accept.
        """
        if not self.storage_account_url:
            return ""
        host = (urlparse(self.storage_account_url).hostname or "").lower()
        if not BLOB_HOST_PATTERN.match(host):
            # Azurite or a custom domain serves both services on one endpoint.
            return self.storage_account_url
        suffix = (
            ".privatelink.table.core.windows.net"
            if ".privatelink." in host
            else ".table.core.windows.net"
        )
        return f"https://{self.resolved_account_name}{suffix}"

    @property
    def document_intelligence_endpoint_looks_valid(self) -> bool:
        if not self.document_intelligence_endpoint:
            return False
        host = (urlparse(self.document_intelligence_endpoint).hostname or "").lower()
        return any(host.endswith(suffix) for suffix in DI_ENDPOINT_SUFFIXES)

    @property
    def uses_legacy_di_endpoint(self) -> bool:
        host = (urlparse(self.document_intelligence_endpoint).hostname or "").lower()
        return any(host.endswith(suffix) for suffix in LEGACY_DI_ENDPOINT_SUFFIXES)

    def config_warnings(self) -> list[str]:
        """Problems that will not stop startup but will break the next request.

        Checked eagerly because a wrong service endpoint produces an Azure error
        that names no setting, so the cause is otherwise invisible.
        """
        warnings: list[str] = []
        if not self.is_configured:
            missing = [
                name
                for name, value in (
                    ("STORAGE_ACCOUNT_URL", self.storage_account_url),
                    ("DOCUMENT_INTELLIGENCE_ENDPOINT", self.document_intelligence_endpoint),
                    ("SOURCE_CONTAINER", self.source_container),
                    ("RESULT_TABLE_NAME", self.result_table_name),
                )
                if not value
            ]
            warnings.append(
                f"Not configured, so every request returns 503. Missing: {', '.join(missing)}."
            )
            return warnings

        if self.uses_legacy_di_endpoint:
            warnings.append(
                f"DOCUMENT_INTELLIGENCE_ENDPOINT is the legacy multi-service host "
                f"{self.document_intelligence_endpoint!r}. That host serves Form Recognizer "
                "v2.1 and returns 404 for the GA /documentintelligence path, so analysis "
                "will fail. Use the resource's own endpoint from the Azure portal: "
                "https://<resource-name>.cognitiveservices.azure.com"
            )
        elif not self.document_intelligence_endpoint_looks_valid:
            warnings.append(
                "DOCUMENT_INTELLIGENCE_ENDPOINT does not look like a Document Intelligence "
                f"resource: {self.document_intelligence_endpoint!r}. Analysis calls will "
                "fail with 'The resource doesn't support specified Http Verb'. Expected "
                "https://<resource-name>.cognitiveservices.azure.com"
            )

        if not self.storage_account_url.startswith("https://"):
            warnings.append(
                f"STORAGE_ACCOUNT_URL is not https: {self.storage_account_url!r}"
            )
        return warnings


    def accepts_blob(self, blob_name: str) -> bool:
        """Guard against a caller pointing this service at an unrelated blob.

        The API has no caller authentication of its own, so the blob name is
        constrained to the configured container and prefix.
        """
        name = blob_name.strip().lstrip("/")
        if not name or ".." in name or not BLOB_NAME_PATTERN.match(name):
            return False
        prefix = self.source_prefix.strip("/")
        return not prefix or name == prefix or name.startswith(f"{prefix}/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
