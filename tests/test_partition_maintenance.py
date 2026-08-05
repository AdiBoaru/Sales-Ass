"""NX-218 — întreținerea partițiilor lunare. ZERO DB (helperi puri + orchestrare monkeypatch-uită).

Crearea reală a partițiilor pe DB (inclusiv refuzul Postgres când DEFAULT-ul conține deja rânduri
din interval) e acoperită de `tests/test_partitions_integration.py` (`@pytest.mark.integration`).
Aici: aritmetica lunilor (inclusiv trecerea de an), numele partițiilor, fail-closed pe
identificatori și comportamentul jobului — creează ce lipsește, sare ce există, un tabel care
crapă nu-l oprește pe celălalt, DEFAULT ne-gol → warning.
"""

from datetime import date

import pytest

from src.db.queries.partitions import (
    default_partition_name,
    month_start,
    next_month,
    partition_name,
)
from src.jobs import partition_maintenance as pm

# --- helperi puri ------------------------------------------------------------


def test_month_start_and_next_month():
    assert month_start(date(2026, 8, 15)) == date(2026, 8, 1)
    assert next_month(date(2026, 8, 1)) == date(2026, 9, 1)


def test_next_month_crosses_year():
    """Decembrie → ianuarie anul următor: bug clasic de aritmetică pe luni."""
    assert next_month(date(2026, 12, 1)) == date(2027, 1, 1)


def test_partition_names_follow_schema_v2_convention():
    assert partition_name("analytics_events", date(2026, 8, 1)) == "analytics_events_2026_08"
    assert partition_name("messages", date(2026, 12, 1)) == "messages_2026_12"
    assert default_partition_name("messages") == "messages_default"


def test_identifier_is_fail_closed():
    with pytest.raises(ValueError):
        partition_name('messages"; drop table x --', date(2026, 8, 1))


def test_target_months_current_plus_ahead():
    assert pm.target_months(date(2026, 12, 20), months_ahead=1) == [
        date(2026, 12, 1),
        date(2027, 1, 1),
    ]
    assert pm.target_months(date(2026, 8, 5), months_ahead=2) == [
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]


# --- ensure_partitions -------------------------------------------------------


def _patch(monkeypatch, *, existing=(), fail_on=(), default_rows=None):
    """Simulează DB-ul: `existing` = partiții deja prezente, `fail_on` = nume care crapă."""
    default_rows = default_rows or {}

    async def fake_create(conn, table, month):
        name = partition_name(table, month)
        if name in fail_on:
            raise RuntimeError("default partition would be violated")
        return name not in existing

    async def fake_default_rows(conn, table):
        return default_rows.get(table, 0)

    monkeypatch.setattr(pm, "create_month_partition", fake_create)
    monkeypatch.setattr(pm, "default_row_count", fake_default_rows)


async def test_creates_current_and_next_month(monkeypatch):
    _patch(monkeypatch)
    out = await pm.ensure_partitions(None, today=date(2026, 8, 5), tables=("analytics_events",))
    assert out["created"] == ["analytics_events_2026_08", "analytics_events_2026_09"]
    assert out["failed"] == []


async def test_existing_partitions_are_skipped(monkeypatch):
    _patch(monkeypatch, existing=("analytics_events_2026_08",))
    out = await pm.ensure_partitions(None, today=date(2026, 8, 5), tables=("analytics_events",))
    assert out["created"] == ["analytics_events_2026_09"]


async def test_idempotent_second_run_creates_nothing(monkeypatch):
    """Rulat de două ori la rând → a doua oară nu mai creează nimic (jobul e zilnic)."""
    _patch(monkeypatch, existing=("messages_2026_08", "messages_2026_09"))
    out = await pm.ensure_partitions(None, today=date(2026, 8, 5), tables=("messages",))
    assert out["created"] == []
    assert out["failed"] == []


async def test_failure_on_one_table_does_not_stop_the_other(monkeypatch):
    """P6: DEFAULT-ul lui analytics_events blochează crearea → messages se asigură oricum."""
    _patch(monkeypatch, fail_on=("analytics_events_2026_08",))
    out = await pm.ensure_partitions(
        None, today=date(2026, 8, 5), tables=("analytics_events", "messages")
    )
    assert out["failed"] == ["analytics_events_2026_08"]
    assert "messages_2026_08" in out["created"]
    assert "messages_2026_09" in out["created"]


async def test_default_rows_are_reported_and_warned(monkeypatch, caplog):
    _patch(monkeypatch, default_rows={"analytics_events": 33})
    with caplog.at_level("WARNING"):
        out = await pm.ensure_partitions(None, today=date(2026, 8, 5), tables=("analytics_events",))
    assert out["default_rows"] == {"analytics_events": 33}
    assert "33 rânduri" in caplog.text


async def test_empty_default_does_not_warn(monkeypatch, caplog):
    _patch(monkeypatch)
    with caplog.at_level("WARNING"):
        out = await pm.ensure_partitions(None, today=date(2026, 8, 5), tables=("messages",))
    assert out["default_rows"] == {"messages": 0}
    assert caplog.text == ""
