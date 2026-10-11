"""The monthly analysis tally that backs the remaining-allowance figure."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import usage as usage_module
from app.usage import UsageError, analyses_this_month, month_key, record_analysis

JANUARY = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
FEBRUARY = datetime(2026, 2, 3, 8, 0, tzinfo=UTC)


def test_month_key_is_the_utc_calendar_month() -> None:
    assert month_key(JANUARY) == "2026-01"


def test_month_key_defaults_to_now() -> None:
    assert month_key() == datetime.now(UTC).strftime("%Y-%m")


def test_no_analysis_has_run_yet(settings, usage_table) -> None:
    assert analyses_this_month(settings, JANUARY) == 0


def test_recording_one_analysis_counts_it(settings, usage_table) -> None:
    record_analysis(settings, JANUARY)
    assert analyses_this_month(settings, JANUARY) == 1


def test_every_run_is_counted(settings, usage_table) -> None:
    for _ in range(3):
        record_analysis(settings, JANUARY)
    assert analyses_this_month(settings, JANUARY) == 3


def test_a_row_is_written_per_run(settings, usage_table) -> None:
    """The count is the number of rows, not a single updated counter."""
    record_analysis(settings, JANUARY)
    record_analysis(settings, JANUARY)
    rows = [key for key in usage_table.entities if key[0] == "2026-01"]
    assert len(rows) == 2


def test_months_do_not_share_a_count(settings, usage_table) -> None:
    record_analysis(settings, JANUARY)
    record_analysis(settings, FEBRUARY)
    record_analysis(settings, FEBRUARY)
    assert analyses_this_month(settings, JANUARY) == 1
    assert analyses_this_month(settings, FEBRUARY) == 2


def test_a_write_failure_is_reported_as_a_usage_error(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenTable:
        def upsert_entity(self, entity: dict) -> None:
            raise RuntimeError("no permission")

    monkeypatch.setattr(usage_module, "table_client", lambda *_a, **_k: BrokenTable())
    with pytest.raises(UsageError, match="count the analysis"):
        record_analysis(settings, JANUARY)


def test_a_read_failure_is_reported_as_a_usage_error(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenTable:
        def query_entities(self, *args: object, **kwargs: object):
            raise RuntimeError("no permission")

    monkeypatch.setattr(usage_module, "table_client", lambda *_a, **_k: BrokenTable())
    with pytest.raises(UsageError, match="read the monthly analysis usage"):
        analyses_this_month(settings, JANUARY)
