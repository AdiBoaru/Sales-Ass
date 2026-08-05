"""NX-217 felia 2 — orchestrarea rollup-ului `demand_daily`. ZERO DB (agregarea e stubbed).

Agregarea SQL reală e testată în `tests/test_demand_rollup_integration.py` (DB reală).
Aici: parsarea zilelor (inclusiv intervalul de backfill), izolarea unei zile care crapă și
contorizarea rândurilor scrise.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from src.jobs import rollup_demand as rd

# --- parse_days --------------------------------------------------------------


def test_parse_days_defaults_to_yesterday():
    expected = (datetime.now(UTC) - timedelta(days=1)).date()
    assert rd.parse_days([]) == [expected]
    assert rd.yesterday_utc() == expected


def test_parse_days_single():
    assert rd.parse_days(["2026-08-04"]) == [date(2026, 8, 4)]


def test_parse_days_inclusive_range():
    """Backfill peste evenimentele deja acumulate: intervalul e INCLUSIV la ambele capete."""
    assert rd.parse_days(["2026-08-01", "2026-08-03"]) == [
        date(2026, 8, 1),
        date(2026, 8, 2),
        date(2026, 8, 3),
    ]


def test_parse_days_rejects_reversed_range():
    with pytest.raises(ValueError):
        rd.parse_days(["2026-08-05", "2026-08-01"])


def test_parse_days_rejects_bad_format():
    with pytest.raises(ValueError):
        rd.parse_days(["nu-e-data"])


# --- run_backfill ------------------------------------------------------------


def _patch(monkeypatch, *, rows=3, fail_on=None):
    seen: list[date] = []

    async def fake_rollup(conn, day):
        seen.append(day)
        if fail_on and day == fail_on:
            raise RuntimeError("boom")
        return rows

    monkeypatch.setattr(rd, "rollup_demand_day", fake_rollup)
    return seen


async def test_backfill_runs_every_day_and_sums_rows(monkeypatch):
    seen = _patch(monkeypatch, rows=4)
    out = await rd.run_backfill(None, days=rd.parse_days(["2026-08-01", "2026-08-03"]))
    assert len(seen) == 3
    assert out == {"days": 3, "rows": 12, "failed": 0}


async def test_backfill_isolates_a_failing_day(monkeypatch):
    """P6: o zi coruptă nu blochează restul intervalului — fiecare zi e o tranzacție proprie."""
    seen = _patch(monkeypatch, rows=2, fail_on=date(2026, 8, 2))
    out = await rd.run_backfill(None, days=rd.parse_days(["2026-08-01", "2026-08-03"]))
    assert len(seen) == 3  # nu s-a oprit la ziua care a crăpat
    assert out == {"days": 3, "rows": 4, "failed": 1}
