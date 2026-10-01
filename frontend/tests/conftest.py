from __future__ import annotations

import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolate_from_local_env(monkeypatch):
    """Keep tests independent of a developer's real .env file.

    Without this, a local .env (real account URLs, blob prefixes, keys) leaks
    into every Settings() built during the test run.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
