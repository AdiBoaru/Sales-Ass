"""NX-329 felia 2b — faptele resolverului pe catalogul REAL (`sole-ro`). Read-only.

Marcate `integration` → excluse din CI. Local:
`NX_TESTS_READ_ENV_FILE=1 pytest tests/test_reference_facts_db.py -m integration -q -s`."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.catalog.reference_facts import fetch_reference_facts
from src.conversation.references import CatalogLookup, name_key
from src.db.connection import close_pool, get_pool, tenant_conn
from tests.tenants import CATALOG_BIZ

pytestmark = pytest.mark.integration


class _Deps:
    def db(self, op):
        @asynccontextmanager
        async def _cm():
            async with tenant_conn(CATALOG_BIZ) as conn:
                yield conn

        return _cm()


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def _two_products() -> list[dict]:
    async with tenant_conn(CATALOG_BIZ) as conn:
        rows = await conn.fetch(
            "select id::text as id, name from products"
            " where business_id = $1 and status = 'active' order by id limit 2",
            CATALOG_BIZ,
        )
    if len(rows) < 2:
        pytest.skip("catalogul tenantului de test are sub două produse")
    return [dict(r) for r in rows]


async def test_real_catalog_revalidates_ids_and_finds_a_full_distinctive_name(pool):
    rows = await _two_products()
    distinctive = rows[0]["name"].split(" - ", 1)[0].split(",", 1)[0].strip()
    foreign = "00000000-0000-0000-0000-000000000000"
    lookup = CatalogLookup(
        ids=(rows[0]["id"], rows[1]["id"], foreign),
        names=(distinctive,),
        attributes=("product_type",),
    )
    facts = await fetch_reference_facts(_Deps(), CATALOG_BIZ, lookup)

    assert {rows[0]["id"], rows[1]["id"]} <= set(facts.products)
    assert foreign not in facts.products
    assert all(f.price is None or f.price > 0 for f in facts.products.values())
    if len(distinctive) >= 8:
        hits = facts.named.get(name_key(distinctive), ())
        assert rows[0]["id"] in {pid for pid, _ in hits}
