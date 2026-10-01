"""Store the submitted photo in Blob Storage.

The service owns the blob name. A caller never chooses one, so a submission
cannot point this service at an unrelated blob, and the name is derived from the
content hash: resubmitting the same photo overwrites the same blob rather than
filling the container with near-duplicates.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContentSettings

from app.clients import azure_error_message, blob_service
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DEFAULT_EXTENSION = ".jpg"

STORAGE_HINT = (
    "Check STORAGE_ACCOUNT_URL, and that the identity has Storage Blob Data "
    "Contributor on the container."
)


class PhotoStorageError(RuntimeError):
    """Raised when the photo could not be written to blob storage."""


@dataclass(frozen=True)
class StoredBlob:
    name: str
    url: str


def content_digest(photo: bytes) -> str:
    """Stable identity for a photo's bytes."""
    return hashlib.sha256(photo).hexdigest()


def build_blob_name(digest: str, content_type: str, settings: Settings) -> str:
    """Content-addressed blob name: same bytes always give the same name.

    The hash is truncated to 32 hex characters, which is what the invoice id is
    derived from, so the two stay consistent.
    """
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type, DEFAULT_EXTENSION)
    prefix = settings.source_prefix.strip("/")
    return f"{prefix}/{digest[:32]}{extension}" if prefix else f"{digest[:32]}{extension}"


def store_photo(
    photo: bytes,
    content_type: str,
    settings: Settings | None = None,
) -> StoredBlob:
    """Write ``photo`` to the source container and return its name and URL."""
    settings = settings or get_settings()
    blob_name = build_blob_name(content_digest(photo), content_type, settings)
    try:
        container = blob_service(settings).get_container_client(
            settings.source_container
        )
        container.upload_blob(
            name=blob_name,
            data=photo,
            # Same bytes, same name, so overwrite keeps the write idempotent
            # without generating a new name per retry.
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
    except ResourceExistsError as exc:  # pragma: no cover - overwrite=True avoids this
        raise PhotoStorageError(f"Blob {blob_name} already exists.") from exc
    except (AzureError, ValueError) as exc:
        # ValueError covers SDK configuration errors, e.g. a token credential
        # pointed at a non-HTTPS endpoint.
        logger.exception("Failed to store blob %s", blob_name)
        raise PhotoStorageError(azure_error_message(exc, "store the photo", STORAGE_HINT)) from exc

    logger.info(
        "Stored blob %s in container %s", blob_name, settings.source_container
    )
    return StoredBlob(name=blob_name, url=f"{container.url}/{blob_name}")


def delete_photo(settings: Settings, blob_name: str) -> None:
    """Remove a stored photo, for when the reviewer chooses not to keep it.

    Scoped the same way as the read: only a blob this service would analyse is
    ever deleted, so a corrupted row cannot be used to remove something else
    from the container.
    """
    if not settings.accepts_blob(blob_name):
        raise PhotoStorageError(
            "Refusing to delete a photo outside "
            f"{settings.source_container}"
            + (f"/{settings.source_prefix}" if settings.source_prefix else "")
            + "."
        )
    try:
        blob = blob_service(settings).get_blob_client(
            container=settings.source_container, blob=blob_name
        )
        blob.delete_blob()
    except ResourceNotFoundError:
        # Already gone, which is the state the caller asked for.
        logger.info("Photo %s was already absent", blob_name)
        return
    except (AzureError, ValueError) as exc:
        logger.exception("Failed to delete blob %s", blob_name)
        raise PhotoStorageError(
            azure_error_message(exc, "delete the photo", STORAGE_HINT)
        ) from exc
    logger.info("Deleted blob %s", blob_name)
