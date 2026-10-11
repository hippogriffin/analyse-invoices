"""Monthly analysis usage, so the rest of the allowance can be reported.

Document Intelligence bills per page analysed, so every analysis that runs costs
money whether or not the reviewer keeps the result. The result table records only
accepted invoices, which cannot answer "how much of the month's allowance is
left", so the count lives in its own table: one row per analysis run, under a
partition named for the month.

Rows are only ever added, never amended. A read-modify-write counter would need
concurrency control, because two analyses finishing at the same moment would race
for the same row; an append-only log has nothing to contend for, and the count for
a month is simply the number of rows in its partition. Partition keys are the
month (``2026-10``), so the rows for one month are a single partition scan and
never mix with another month's.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.clients import azure_error_message, table_client
from app.config import Settings

logger = logging.getLogger(__name__)

USAGE_HINT = (
    "Check STORAGE_ACCOUNT_URL and USAGE_TABLE_NAME: the table is created at "
    "startup, and the identity needs Storage Table Data Contributor."
)


class UsageError(RuntimeError):
    """Raised when the monthly usage cannot be read or written."""


def month_key(when: datetime | None = None) -> str:
    """The calendar month an analysis belongs to, as ``YYYY-MM`` in UTC."""
    return (when or datetime.now(UTC)).strftime("%Y-%m")


def record_analysis(settings: Settings, when: datetime | None = None) -> None:
    """Count one analysis run against its month's allowance.

    One row per run rather than an incremented counter: the write is a plain
    insert, so two analyses finishing at once cannot lose one another's
    increment, and a retried write cannot count a run twice the way re-reading a
    counter and writing it back could.
    """
    table = table_client(settings, settings.usage_table_name)
    entity = {
        "PartitionKey": month_key(when),
        # Only unique within the month, which is all a row key has to be. The
        # value is never read back, so a random suffix is enough.
        "RowKey": uuid.uuid4().hex,
        "recordedAt": (when or datetime.now(UTC)).isoformat(timespec="seconds"),
    }
    try:
        table.upsert_entity(entity)
    except Exception as exc:
        raise UsageError(
            azure_error_message(
                exc, "count the analysis against the monthly allowance", USAGE_HINT
            )
        ) from exc


def analyses_this_month(settings: Settings, when: datetime | None = None) -> int:
    """How many analyses have run in the same month as ``when`` (default now)."""
    table = table_client(settings, settings.usage_table_name)
    try:
        rows = table.query_entities(
            "PartitionKey eq @month", parameters={"month": month_key(when)}
        )
        return sum(1 for _ in rows)
    except Exception as exc:
        raise UsageError(
            azure_error_message(exc, "read the monthly analysis usage", USAGE_HINT)
        ) from exc
