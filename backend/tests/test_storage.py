"""Storing the submitted photo in Blob Storage."""

from __future__ import annotations

import hashlib

import pytest

from app.storage import (
    CONTENT_TYPE_EXTENSIONS,
    PhotoStorageError,
    build_blob_name,
    content_digest,
    delete_photo,
    store_photo,
)

PHOTO = b"\xff\xd8\xff-not-a-real-jpeg"


def test_digest_is_the_sha256_of_the_bytes() -> None:
    assert content_digest(PHOTO) == hashlib.sha256(PHOTO).hexdigest()


def test_digest_is_stable() -> None:
    assert content_digest(PHOTO) == content_digest(PHOTO)


class TestBlobName:
    def test_is_derived_from_the_content(self, settings) -> None:
        digest = content_digest(PHOTO)
        assert build_blob_name(digest, "image/jpeg", settings) == (
            f"2026/{digest[:32]}.jpg"
        )

    def test_same_content_always_gives_the_same_name(self, settings) -> None:
        digest = content_digest(PHOTO)
        assert build_blob_name(digest, "image/png", settings) != ""
        # Identical bytes plus identical type, so identical name.
        assert build_blob_name(digest, "image/jpeg", settings) == build_blob_name(
            digest, "image/jpeg", settings
        )

    @pytest.mark.parametrize("content_type", sorted(CONTENT_TYPE_EXTENSIONS))
    def test_extension_matches_the_content_type(self, settings, content_type) -> None:
        name = build_blob_name(content_digest(PHOTO), content_type, settings)
        assert name.endswith(CONTENT_TYPE_EXTENSIONS[content_type])

    def test_an_unknown_type_still_gets_an_extension(self, settings) -> None:
        name = build_blob_name("deadbeef", "application/octet-stream", settings)
        assert name.endswith(".jpg")

    def test_no_prefix_means_no_directory(self) -> None:
        from app.config import Settings

        settings = Settings(source_prefix="")
        assert "/" not in build_blob_name("deadbeef", "image/jpeg", settings)

    def test_surrounding_slashes_in_the_prefix_are_ignored(self) -> None:
        from app.config import Settings

        settings = Settings(source_prefix="/2026/")
        assert build_blob_name("deadbeef", "image/jpeg", settings) == (
            "2026/deadbeef.jpg"
        )

    def test_the_name_is_a_valid_blob_name(self, settings) -> None:
        """accepts_blob is what the analysis read path trusts."""
        name = build_blob_name(content_digest(PHOTO), "image/jpeg", settings)
        assert settings.accepts_blob(name)

    def test_a_generated_name_is_always_in_scope(self, settings) -> None:
        """Every content type must yield a name the analysis read path accepts.

        The name is built from a sha256 hex digest, so this holds for any input:
        the digest cannot contain a separator, a dot or a traversal.
        """
        for content_type in CONTENT_TYPE_EXTENSIONS:
            for photo in (PHOTO, b"", b"\x00" * 4096, bytes(range(256))):
                name = build_blob_name(content_digest(photo), content_type, settings)
                assert settings.accepts_blob(name), name
                digest = content_digest(photo)[:32]
                assert name == f"2026/{digest}{CONTENT_TYPE_EXTENSIONS[content_type]}"


class TestStorePhoto:
    def test_writes_the_bytes_it_was_given(self, settings, blob_container) -> None:
        stored = store_photo(PHOTO, "image/jpeg", settings)
        assert blob_container.blobs == {stored.name: PHOTO}

    def test_sets_the_content_type(self, settings, blob_container) -> None:
        stored = store_photo(PHOTO, "image/png", settings)
        assert blob_container.content_types[stored.name] == "image/png"

    def test_the_returned_url_points_at_the_blob(self, settings, blob_container) -> None:
        stored = store_photo(PHOTO, "image/jpeg", settings)
        assert stored.url == f"{blob_container.url}/{stored.name}"

    def test_resubmitting_the_same_photo_overwrites_in_place(
        self, settings, blob_container
    ) -> None:
        first = store_photo(PHOTO, "image/jpeg", settings)
        second = store_photo(PHOTO, "image/jpeg", settings)
        assert first.name == second.name
        assert list(blob_container.blobs) == [first.name]

    def test_a_storage_failure_is_wrapped(self, settings, monkeypatch) -> None:
        from azure.core.exceptions import HttpResponseError

        from app import storage as storage_module
        from app.storage import PhotoStorageError

        class Failing:
            url = "https://acct.blob.core.windows.net/pictures"

            def get_container_client(self, _name: str) -> object:
                class Container:
                    url = "https://acct.blob.core.windows.net/pictures"

                    def upload_blob(self, **_kwargs: object) -> None:
                        raise HttpResponseError("the account key is wrong")

                return Container()

        monkeypatch.setattr(
            storage_module, "blob_service", lambda *_a, **_k: Failing()
        )
        with pytest.raises(PhotoStorageError) as caught:
            store_photo(PHOTO, "image/jpeg", settings)
        # The detail is safe to return to the caller, and says what to fix.
        assert "account key" in str(caught.value)

    def test_an_sdk_configuration_error_is_wrapped(self, settings, monkeypatch) -> None:
        """A non-HTTPS endpoint raises ValueError inside the SDK, not AzureError."""
        from app import storage as storage_module
        from app.storage import PhotoStorageError

        class Failing:
            url = ""

            def get_container_client(self, _name: str) -> object:
                class Container:
                    url = ""

                    def upload_blob(self, **_kwargs: object) -> None:
                        raise ValueError("Token credential requires HTTPS.")

                return Container()

        monkeypatch.setattr(
            storage_module, "blob_service", lambda *_a, **_k: Failing()
        )
        with pytest.raises(PhotoStorageError):
            store_photo(PHOTO, "image/jpeg", settings)


class TestDeletePhoto:
    def test_removes_the_blob(self, settings, blob_container) -> None:
        store_photo(PHOTO, "image/jpeg", settings)
        name = build_blob_name(content_digest(PHOTO), "image/jpeg", settings)
        assert name in blob_container.blobs

        delete_photo(settings, name)

        assert name not in blob_container.blobs
        assert name not in blob_container.content_types

    def test_an_already_absent_photo_is_not_an_error(self, settings) -> None:
        """Deleting twice is what a retried request looks like, so it must pass."""
        delete_photo(settings, "2026/never-existed.jpg")

    def test_a_blob_outside_the_configured_prefix_is_refused(self, settings) -> None:
        """A corrupted row must not be able to delete something else."""
        with pytest.raises(PhotoStorageError, match="Refusing to delete"):
            delete_photo(settings, "elsewhere/someone-elses-photo.jpg")

    def test_a_traversing_name_is_refused(self, settings) -> None:
        with pytest.raises(PhotoStorageError, match="Refusing to delete"):
            delete_photo(settings, "2026/../../other/photo.jpg")

    def test_an_empty_name_is_refused(self, settings) -> None:
        with pytest.raises(PhotoStorageError, match="Refusing to delete"):
            delete_photo(settings, "   ")

    def test_a_storage_failure_is_wrapped(self, settings, monkeypatch) -> None:
        from azure.core.exceptions import HttpResponseError

        from app import storage as storage_module

        class Failing:
            def get_blob_client(self, **_kwargs: object) -> object:
                class Blob:
                    def delete_blob(self) -> None:
                        raise HttpResponseError("boom")

                return Blob()

        monkeypatch.setattr(
            storage_module, "blob_service", lambda *_a, **_k: Failing()
        )
        with pytest.raises(PhotoStorageError, match="delete the photo"):
            delete_photo(settings, "2026/abc.jpg")
