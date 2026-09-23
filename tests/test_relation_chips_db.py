"""NX-316 felia 3 — chips din graf pe catalogul REAL (`sole-ro`). Read-only.

Conversația reală «vreau o crema de hidratare» → «cum se folosește prima»: produsul discutat e SOME
BY MI Yuja Niacin. DoD-ul cardului: dacă graful are muchii pentru el, turul 3 oferă un pas de rutină
(`routine_next`); altfel măcar nu mai oferă „Machiaj".

Marcate `integration` → excluse din CI. Local:
`pytest tests/test_relation_chips_db.py -m integration -q -s`.
"""

from __future__ import annotations

import pytest

from src.conversation import chip_moves
from src.db.connection import close_pool, get_pool, tenant_conn
from src.db.queries.catalog import related_in_stock, relation_type_counts
from tests.tenants import CATALOG_BIZ

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def _anchor() -> dict:
    async with tenant_conn(CATALOG_BIZ) as conn:
        row = await conn.fetchrow(
            "select id::text as id, name, price::float as price,"
            " attributes->>'product_type' as product_type from products"
            " where business_id = $1 and status = 'active' and name ilike '%Yuja Niacin%'"
            " order by id limit 1",
            CATALOG_BIZ,
        )
    if row is None:
        pytest.skip("catalogul nu mai are crema din conversația reală")
    return {
        "product_id": row["id"],
        "name": row["name"],
        "price": row["price"],
        "attributes": {"product_type": row["product_type"]},
    }


async def test_one_query_gives_the_graph_moves_of_the_real_turn(pool):
    card = await _anchor()
    async with tenant_conn(CATALOG_BIZ) as conn:
        rows = await relation_type_counts(conn, CATALOG_BIZ, [card["product_id"]])
    assert all(r["anchor_id"] == card["product_id"] for r in rows)
    assert all(r["kind"] in ("routine_next", "complement", "substitute") for r in rows)
    assert all(r["n"] > 0 for r in rows)

    types = chip_moves.relation_types_wanted(rows, [card])
    print(f"\nrânduri={len(rows)} tipuri de rutină={types}")
    if not types:
        pytest.skip("graful nu are pași de rutină servabili pentru produsul din conversație")
    # Fraza ar veni din vocabular; aici ajunge cheia însăși, suficient pentru construcție.
    moves = chip_moves.from_relations(rows, [card], {t: t.replace("_", " ") for t in types})
    kinds = {m.kind for m in moves}
    print("mutări:", sorted(m.move_id for m in moves))
    assert "routine_next" in kinds


async def test_pressing_routine_next_serves_that_type_in_stock(pool):
    card = await _anchor()
    async with tenant_conn(CATALOG_BIZ) as conn:
        rows = await relation_type_counts(conn, CATALOG_BIZ, [card["product_id"]])
        types = chip_moves.relation_types_wanted(rows, [card])
        if not types:
            pytest.skip("graful nu are pași de rutină servabili")
        got = await related_in_stock(
            conn,
            CATALOG_BIZ,
            card["product_id"],
            ("routine_next", "complement"),
            product_type=types[0],
        )
    assert got, "numărătoarea promitea produse, handlerul trebuie să le găsească"
    assert all((p["attributes"] or {}).get("product_type") == types[0] for p in got)
    assert all(p["availability"] in ("in_stock", "low_stock") for p in got)
    assert card["product_id"] not in {p["id"] for p in got}


async def test_similar_to_serves_substitutes_in_stock(pool):
    card = await _anchor()
    async with tenant_conn(CATALOG_BIZ) as conn:
        rows = await relation_type_counts(conn, CATALOG_BIZ, [card["product_id"]], ("substitute",))
        if not rows:
            pytest.skip("produsul n-are substitute servabile")
        got = await related_in_stock(conn, CATALOG_BIZ, card["product_id"], ("substitute",))
    assert got and all(p["availability"] in ("in_stock", "low_stock") for p in got)


async def test_another_tenant_sees_nothing(pool):
    card = await _anchor()
    other = "00000000-0000-0000-0000-000000000000"
    async with tenant_conn(other) as conn:
        rows = await relation_type_counts(conn, other, [card["product_id"]])
        got = await related_in_stock(conn, other, card["product_id"], ("substitute",))
    assert rows == [] and got == []
