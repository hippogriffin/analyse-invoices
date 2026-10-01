"""Azure client factories.

Credentials are cached on the settings object so the token cache is shared
across requests. Settings is frozen and therefore hashable.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from azure.core.credentials import (
    AzureKeyCredential,
    AzureNamedKeyCredential,
    TokenCredential,
)
from azure.data.tables import TableClient, TableServiceClient
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

from app.config import AuthMethod, Settings, get_settings

logger = logging.getLogger(__name__)


class NotConfiguredError(RuntimeError):
    """Raised when a required setting is missing."""


class StorageWriteError(RuntimeError):
    """Raised when a Table Storage operation fails."""


# Azure returns this when the verb does not fit the resource that was addressed,
# which in practice almost always means an endpoint from the wrong service.
VERB_ERROR = "doesn't support specified Http Verb"


def azure_error_message(exc: Exception, action: str, hint: str = "") -> str:
    """Describe an Azure failure in terms the operator can act on."""
    status = getattr(exc, "status_code", None)
    message = f"Could not {action}"
    if status:
        message += f" (HTTP {status})"
    message += f": {exc}"
    if hint and VERB_ERROR in str(exc):
        message += f" {hint}"
    return message


@lru_cache
def data_credential(settings: Settings) -> TokenCredential:
    """Credential for Blob Storage and Table Storage."""
    if settings.blob_auth_method is AuthMethod.ACCOUNT_KEY:
        return AzureNamedKeyCredential(
            settings.resolved_account_name,
            settings.azure_storage_account_key.get_secret_value(),
        )
    if settings.azure_client_id:
        return ManagedIdentityCredential(client_id=settings.azure_client_id)
    return ManagedIdentityCredential()


@lru_cache
def document_intelligence_credential(
    settings: Settings,
) -> AzureKeyCredential | TokenCredential:
    """Credential for Document Intelligence.

    Document Intelligence takes a token credential in Azure and an
    ``AzureKeyCredential`` (sent as the ``api-key`` header) for local testing.
    """
    if settings.uses_account_key:
        return AzureKeyCredential(
            settings.document_intelligence_key.get_secret_value()
        )
    if settings.azure_client_id:
        return ManagedIdentityCredential(client_id=settings.azure_client_id)
    return ManagedIdentityCredential()


def blob_service(settings: Settings | None = None) -> BlobServiceClient:
    settings = settings or get_settings()
    if not settings.storage_account_url:
        raise NotConfiguredError("STORAGE_ACCOUNT_URL is not set.")
    return BlobServiceClient(
        account_url=settings.storage_account_url,
        credential=data_credential(settings),
    )


def table_service(settings: Settings | None = None) -> TableServiceClient:
    settings = settings or get_settings()
    if not settings.table_storage_url:
        raise NotConfiguredError(
            "STORAGE_ACCOUNT_URL is not set, so the Table Storage endpoint is unknown."
        )
    return TableServiceClient(
        # Not the blob endpoint: table operations must go to the table host.
        endpoint=settings.table_storage_url,
        credential=data_credential(settings),
    )


def table_client(settings: Settings | None = None) -> TableClient:
    return table_service(settings).get_table_client(settings.result_table_name)


def ensure_table(settings: Settings | None = None) -> None:
    """Create the results table if it is missing.

    The failure is logged rather than raised: startup should still complete, and
    the first write reports the real cause. It used to be swallowed at debug
    level, which made a missing table look like a successful setup.
    """
    settings = settings or get_settings()
    try:
        # create_table_if_not_exists is on the service client, not the table client.
        table_service(settings).create_table_if_not_exists(settings.result_table_name)
    except Exception:
        logger.warning(
            "Could not create table %r; writes will fail until it exists",
            settings.result_table_name,
            exc_info=True,
        )
