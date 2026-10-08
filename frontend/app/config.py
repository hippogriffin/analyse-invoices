"""Application configuration, read from environment variables.

This service holds no Azure credentials and no storage access. It renders the
UI and hands the chosen photo to the analysis backend over HTTP, so everything
here is about where that backend lives and what the UI should offer the user.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Invoice analysis -----------------------------------------------------
    analysis_api_base_url: str = Field(
        default="",
        description=(
            "Base URL of the invoice analysis service. The photo is posted "
            "here; this service never touches storage or Document Intelligence "
            "itself. When empty the UI can capture a photo but cannot submit it."
        ),
    )

    # Network. The default binds every interface so the UI is reachable from
    # any IP, not just the loopback address. Override HOST to lock it down.
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)

    # UI limits. The backend enforces the same rules authoritatively; these
    # exist so an unusable photo is rejected in the browser instead of after an
    # upload. Keep them in step with the backend's MAX_UPLOAD_BYTES and
    # ALLOWED_CONTENT_TYPES.
    max_upload_bytes: int = Field(default=10 * 1024 * 1024)
    # NoDecode: the value arrives as a raw comma-separated string from env/.env
    # and is split by the validator below, instead of being JSON-decoded.
    allowed_content_types: Annotated[
        tuple[str, ...], NoDecode, Field(default=DEFAULT_ALLOWED_CONTENT_TYPES)
    ]

    @field_validator("analysis_api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # The UI appends "/api/invoices", so a trailing slash would double up.
        return value.strip().rstrip("/")

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
        """Compared against the file's own type, so case is normalised."""
        return tuple(item.lower() for item in value)

    @model_validator(mode="after")
    def _check_configuration(self) -> Settings:
        if not self.analysis_api_base_url:
            logger.warning(
                "ANALYSIS_API_BASE_URL is not set, so the UI cannot submit a photo. "
                "This service has no storage of its own, so there is nothing else "
                "it could fall back to."
            )
        return self

    @property
    def is_analysis_configured(self) -> bool:
        return bool(self.analysis_api_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
