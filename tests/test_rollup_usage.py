"""F2-3 — orchestrarea rollup-ului usage_daily. ZERO DB (query-urile monkeypatch-uite).

Agregarea SQL reală (`rollup_usage_day`) e testată separat cu DB (`@pytest.mark.integration`).
Aici: iterarea businessurilor + izolarea unui business care crapă + parsarea zilei din argv.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from src.jobs import rollup_usage as ru

# --- parse_day / yesterday ---------------------------------------------------


def test_parse_day_from_arg():
    assert ru.parse_day(["2026-06-15"]) == date(2026, 6, 15)


def test_parse_day_defaults_to_yesterday():
    expected = (datetime.now(UTC) - timedelta(days=1)).date()
    assert ru.parse_day([]) == expected
    assert ru.yesterday_utc() == expected


def test_parse_day_invalid_raises():
    with pytest.raises(ValueError):
        ru.parse_day(["nu-e-data"])


# --- run_rollup --------------------------------------------------------------


def _patch(monkeypatch, business_ids, *, fail_on=None):
    calls: list[str] = []

    async def fake_list(conn):
        return business_ids

    async def fake_rollup(conn, business_id, day):
        calls.append(business_id)
        if fail_on and business_id == fail_on:
            raise RuntimeError("boom")
        return {"business_id": business_id, "day": day}

    monkeypatch.setattr(ru, "list_active_business_ids", fake_list)
    monkeypatch.setattr(ru, "rollup_usage_day", fake_rollup)
    return calls


async def test_run_rollup_processes_all_businesses(monkeypatch):
    calls = _patch(monkeypatch, ["b1", "b2", "b3"])
    res = await ru.run_rollup(object(), day=date(2026, 6, 15))
    assert res == {"processed": 3, "failed": 0}
    assert calls == ["b1", "b2", "b3"]


async def test_run_rollup_skips_failing_business(monkeypatch):
    calls = _patch(monkeypatch, ["b1", "b2", "b3"], fail_on="b2")
    res = await ru.run_rollup(object(), day=date(2026, 6, 15))
    assert res == {"processed": 2, "failed": 1}
    assert calls == ["b1", "b2", "b3"]  # b3 rulează în ciuda eșecului lui b2


async def test_run_rollup_no_businesses(monkeypatch):
    _patch(monkeypatch, [])
    res = await ru.run_rollup(object(), day=date(2026, 6, 15))
    assert res == {"processed": 0, "failed": 0}
