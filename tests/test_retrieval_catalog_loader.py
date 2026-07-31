"""NX-203 — încărcătorul de catalog pentru evaluarea constrângerilor.

Testul cu `attributes` ca STRING nu e teoretic: asyncpg întoarce `jsonb` ca text (nu e înregistrat
niciun codec în afară de `vector`). Fără decodare, fiecare atribut ar ieşi „necunoscut", deci nicio
constrângere n-ar fi verificată — iar raportul ar arăta ca o rulare curată.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.db.queries.catalog import _EFFECTIVE_PRICE
from src.evals.retrieval.catalog import load_catalog
from src.evals.retrieval.constraints import VIOLATES, evaluate


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows


def _row(**kw):
    base = {
        "id": "p-1",
        "name": "Cremă test",
        "price": 70,
        "attributes": {},
        "list_price": 70,
        "category_slug": "creme-fata",
        "updated_at": dt.datetime(2026, 7, 30, 12, 0, 0),
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_jsonb_string_attributes_are_decoded():
    conn = _FakeConn([_row(attributes='{"fragrance_free": false, "spf": 30}')])
    catalog = await load_catalog(conn, "biz")

    product = catalog.products["p-1"]
    assert product["attributes"] == {"fragrance_free": False, "spf": 30}
    # şi, mai important, constrângerea chiar se evaluează pe ele
    assert evaluate(product, {"facet": "fragrance_free", "op": "eq", "value": True}) == VIOLATES


@pytest.mark.asyncio
async def test_filters_active_and_published():
    """Acelaşi filtru ca la derivarea excepţiilor; altfel cele două ar vorbi despre cataloage
    diferite fără ca discrepanţa să apară undeva ca eroare."""
    conn = _FakeConn([_row()])
    await load_catalog(conn, "biz")

    sql, args = conn.calls[0]
    assert "p.status = 'active'" in sql
    assert "p.content_status = 'published'" in sql
    assert args == ("biz",)


@pytest.mark.asyncio
async def test_projects_effective_price_from_production_expression():
    """Preţul proiectat e cel EFECTIV, prin chiar expresia din calea de producţie.

    Verificarea e pe SQL fiindcă divergenţa se naşte în proiecţie; efectul real (100 de listă, 60
    efectiv, prag 90 → satisfies) e testat pe DB în `test_retrieval_effective_price.py`."""
    conn = _FakeConn([_row()])
    await load_catalog(conn, "biz")

    sql, _args = conn.calls[0]
    assert _EFFECTIVE_PRICE in sql, "snapshotul trebuie să poarte preţul pe care îl vede clientul"
    assert "product_variants" in sql, "minimul variantelor face parte din preţul efectiv"
    assert "sale_start" in sql and "sale_end" in sql, "fereastra promoţiei, nu doar sale_price"


@pytest.mark.asyncio
async def test_version_tracks_latest_update_not_only_count():
    """Un re-seed care schimbă preţuri fără să adauge produse trebuie să dea altă versiune."""
    older = await load_catalog(_FakeConn([_row()]), "biz")
    newer = await load_catalog(
        _FakeConn([_row(price=240, updated_at=dt.datetime(2026, 7, 31, 9, 0, 0))]), "biz"
    )

    assert len(older.products) == len(newer.products)
    assert older.version != newer.version
    assert older.fingerprint != newer.fingerprint


@pytest.mark.asyncio
async def test_same_second_price_change_still_changes_fingerprint():
    """Coliziunea reală: `version` trunchiază `updated_at` la secundă.

    Două modificări de preţ în aceeaşi secundă dau versiuni identice — dacă amprenta n-ar acoperi
    conţinutul, două cataloage diferite ar produce `live:1@...:acelaşi_hash`, iar comparaţia
    baseline-candidat ar accepta rapoarte incomparabile."""
    stamp = dt.datetime(2026, 7, 31, 12, 0, 0)
    a = await load_catalog(_FakeConn([_row(price=70, updated_at=stamp)]), "biz")
    b = await load_catalog(_FakeConn([_row(price=240, updated_at=stamp)]), "biz")

    assert a.version == b.version
    assert a.fingerprint != b.fingerprint
